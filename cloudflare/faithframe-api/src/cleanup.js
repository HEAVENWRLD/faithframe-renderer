const HOUR = 60 * 60 * 1000;
const DAY = 24 * HOUR;
const SUCCESS_MEDIA_RETENTION_MS = 48 * HOUR;
const FAILED_MEDIA_RETENTION_MS = 14 * DAY;
const REVIEW_MEDIA_RETENTION_MS = 60 * DAY;
const ORPHAN_MEDIA_RETENTION_MS = 24 * HOUR;
const MAX_DELETE_OBJECTS_PER_RUN = 300;
const MAX_LIST_PAGES = 30;
const LIST_PAGE_SIZE = 1000;

function isoMs(value) {
  const parsed = Date.parse(String(value || ""));
  return Number.isFinite(parsed) ? parsed : 0;
}

function uuidFromTaskKey(key) {
  const match = String(key || "").match(/^tasks\/([0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\/task\.json$/i);
  return match ? match[1] : null;
}

function uuidFromJobKey(key) {
  const match = String(key || "").match(/^jobs\/([0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\//i);
  return match ? match[1] : null;
}

async function listAll(bucket, prefix) {
  const objects = [];
  let cursor;
  let pages = 0;
  do {
    const page = await bucket.list({ prefix, limit: LIST_PAGE_SIZE, cursor });
    objects.push(...(page.objects || []));
    pages += 1;
    if (!page.truncated) return { objects, complete: true, pages };
    cursor = page.cursor;
  } while (cursor && pages < MAX_LIST_PAGES);
  return { objects, complete: false, pages };
}

async function removePrefix(bucket, prefix, budget) {
  let cursor;
  let pages = 0;
  let deletedObjects = 0;
  let deletedBytes = 0;
  let more = false;
  do {
    const page = await bucket.list({ prefix, limit: LIST_PAGE_SIZE, cursor });
    const objects = page.objects || [];
    const remaining = Math.max(0, budget - deletedObjects);
    const batch = objects.slice(0, remaining);
    for (let index = 0; index < batch.length; index += 25) {
      const group = batch.slice(index, index + 25);
      await Promise.all(group.map(object => bucket.delete(object.key)));
      deletedObjects += group.length;
      deletedBytes += group.reduce((sum, object) => sum + Math.max(0, Number(object.size || 0)), 0);
    }
    if (batch.length < objects.length || deletedObjects >= budget && page.truncated) {
      more = true;
      break;
    }
    pages += 1;
    if (!page.truncated) break;
    cursor = page.cursor;
  } while (cursor && pages < MAX_LIST_PAGES);
  if (pages >= MAX_LIST_PAGES && cursor) more = true;
  return { deletedObjects, deletedBytes, more };
}

async function deleteExact(bucket, key) {
  const object = await bucket.head(key);
  if (!object) return { deletedObjects: 0, deletedBytes: 0 };
  await bucket.delete(key);
  return { deletedObjects: 1, deletedBytes: Math.max(0, Number(object.size || 0)) };
}

function ageEligibleTask(task, now) {
  if (task.storageCleanup && task.storageCleanup.mediaDeletedAt) {
    return { eligible: false, reason: "media_already_cleaned" };
  }
  const taskId = String(task.taskId || "");
  const hasYoutubeVideo = Boolean(task.youtubeVideoId) || task.youtubeStatus === "published";
  const hasActiveUploadSession = Boolean(task.youtubeUploadSessionUrl) && !hasYoutubeVideo;
  if (hasActiveUploadSession) return { eligible: false, reason: "resumable_upload_session_protected" };
  if (["upload_queued", "uploading", "upload_retryable"].includes(String(task.youtubeStatus || ""))) {
    return { eligible: false, reason: "youtube_upload_in_progress_or_retryable" };
  }

  if (hasYoutubeVideo) {
    const completedAt = isoMs(task.youtubePublishedAt) || isoMs(task.updatedAt) || isoMs(task.createdAt);
    return now - completedAt >= SUCCESS_MEDIA_RETENTION_MS
      ? { eligible: true, kind: "published", reason: "youtube_upload_confirmed" }
      : { eligible: false, reason: "published_video_retention_window" };
  }

  const targetAt = isoMs(task.targetPublishAt);
  if (targetAt > now) return { eligible: false, reason: "future_publish_target_protected" };

  const status = String(task.youtubeStatus || "");
  if (status === "needs_review" || status === "uploaded_private") {
    const reference = isoMs(task.updatedAt) || isoMs(task.createdAt);
    return now - reference >= REVIEW_MEDIA_RETENTION_MS
      ? { eligible: true, kind: "expired_review", reason: "review_media_retention_elapsed" }
      : { eligible: false, reason: "manual_review_retention_window" };
  }

  if (["failed", "dispatch_failed", "expired"].includes(String(task.status || ""))) {
    const reference = isoMs(task.updatedAt) || isoMs(task.createdAt);
    return now - reference >= FAILED_MEDIA_RETENTION_MS
      ? { eligible: true, kind: "failed", reason: "failed_job_retention_elapsed" }
      : { eligible: false, reason: "failed_job_retention_window" };
  }

  if (task.status === "ready" && status === "pending") {
    const reference = Math.max(isoMs(task.targetPublishAt), isoMs(task.updatedAt), isoMs(task.createdAt));
    return now - reference >= FAILED_MEDIA_RETENTION_MS
      ? { eligible: true, kind: "expired_ready", reason: "ready_video_not_published_within_retention" }
      : { eligible: false, reason: "ready_video_retention_window" };
  }

  const lastUpdate = isoMs(task.updatedAt) || isoMs(task.createdAt);
  if (["queued", "queued_media", "generating_media", "rendering"].includes(String(task.status || "")) && now - lastUpdate >= 7 * DAY) {
    return { eligible: false, reason: "stale_render_requires_failure_grace_period" };
  }
  return { eligible: false, reason: "task_is_active_or_not_expired" };
}

async function cleanupTask(bucket, task, taskKey, kind, budget) {
  const taskId = String(task.taskId || "");
  const deleted = { objects: 0, bytes: 0 };
  const source = await removePrefix(bucket, "jobs/" + taskId + "/", Math.max(0, budget - deleted.objects));
  deleted.objects += source.deletedObjects;
  deleted.bytes += source.deletedBytes;
  if (source.more) return { deleted, complete: false };

  for (const key of [
    "tasks/" + taskId + "/rendered.mp4",
    "tasks/" + taskId + "/thumbnail.png"
  ]) {
    if (deleted.objects >= budget) return { deleted, complete: false };
    const result = await deleteExact(bucket, key);
    deleted.objects += result.deletedObjects;
    deleted.bytes += result.deletedBytes;
  }

  const timestamp = new Date().toISOString();
  task.storageCleanup = {
    status: kind,
    mediaDeletedAt: timestamp,
    deletedObjects: deleted.objects,
    deletedBytes: deleted.bytes,
    policy: kind === "published" ? "48 hours after YouTube upload confirmation" : "expired media retention"
  };
  if (kind !== "published") {
    task.status = "expired";
    if (!task.youtubeVideoId) task.youtubeStatus = "expired";
    task.storageCleanup.reason = "Large media assets expired; compact task metadata retained.";
  }
  task.updatedAt = timestamp;
  await bucket.put(taskKey, JSON.stringify(task), {
    httpMetadata: { contentType: "application/json; charset=utf-8", cacheControl: "no-store" }
  });
  return { deleted, complete: true };
}

export async function cleanupMedia(env, options = {}) {
  if (!env.MEDIA_BUCKET) throw new Error("MEDIA_BUCKET is not configured.");
  const dryRun = options.dryRun === true;
  const now = Date.now();
  const taskListing = await listAll(env.MEDIA_BUCKET, "tasks/");
  if (!taskListing.complete) {
    throw new Error("Task listing exceeded the cleanup safety limit. No orphan cleanup was attempted.");
  }

  const taskObjects = taskListing.objects.filter(object => uuidFromTaskKey(object.key));
  const taskIds = new Set(taskObjects.map(object => uuidFromTaskKey(object.key)));
  const summary = {
    ok: true,
    dryRun,
    checkedTaskRecords: taskObjects.length,
    publishedJobsCleaned: 0,
    failedJobsExpired: 0,
    abandonedReadyJobsExpired: 0,
    reviewJobsExpired: 0,
    orphanObjectsDeleted: 0,
    deletedObjects: 0,
    deletedBytes: 0,
    protectedActiveUploads: 0,
    protectedFuturePublishes: 0,
    skippedActiveJobs: 0,
    cleanupLimitReached: false,
    checkedAt: new Date(now).toISOString(),
    retentionPolicy: {
      afterSuccessfulYouTubeUpload: "Delete source images/audio and the local MP4 after 48 hours; keep task metadata.",
      failedOrAbandonedJobs: "Delete media after 14 days; keep task metadata marked expired.",
      needsReviewJobs: "Keep media for 60 days; then delete it unless a resumable YouTube session is saved.",
      orphanMedia: "Delete unreferenced job files after 24 hours.",
      activeUploads: "Never delete media while a resumable YouTube session, queued upload, active upload, or retryable upload exists."
    }
  };

  for (const object of taskObjects) {
    if (summary.deletedObjects >= MAX_DELETE_OBJECTS_PER_RUN) {
      summary.cleanupLimitReached = true;
      break;
    }
    const taskId = uuidFromTaskKey(object.key);
    let task;
    try {
      task = await (await env.MEDIA_BUCKET.get(object.key)).json();
    } catch (_) {
      // A malformed task record is deliberately retained for manual inspection.
      continue;
    }
    if (!task || typeof task !== "object" || String(task.taskId || "") !== taskId) continue;

    const statusNow = String(task.youtubeStatus || "");
    const updatedMs = isoMs(task.updatedAt) || isoMs(task.createdAt);
    const dispatchMs = isoMs(task.youtubeDispatchAt) || updatedMs;
    if (!dryRun && ["queued", "queued_media", "generating_media", "rendering"].includes(String(task.status || "")) &&
        updatedMs > 0 && now - updatedMs >= 7 * DAY) {
      task.status = "failed";
      task.error = "Render task exceeded the seven-day safety window without completing. Media will be retained for a further 14 days before cleanup.";
      task.updatedAt = new Date(now).toISOString();
      await env.MEDIA_BUCKET.put(object.key, JSON.stringify(task), {
        httpMetadata: { contentType: "application/json; charset=utf-8", cacheControl: "no-store" }
      });
      summary.staleRenderJobsMarkedFailed = (summary.staleRenderJobsMarkedFailed || 0) + 1;
      continue;
    }
    const staleQueuedUpload =
      statusNow === "upload_queued" && !task.youtubeUploadSessionUrl && !task.youtubeVideoId &&
      dispatchMs > 0 && now - dispatchMs >= 2 * DAY;
    const staleRetryWithoutSession =
      statusNow === "upload_retryable" && !task.youtubeUploadSessionUrl && !task.youtubeVideoId &&
      updatedMs > 0 && now - updatedMs >= 7 * DAY;
    if (!dryRun && (staleQueuedUpload || staleRetryWithoutSession)) {
      task.youtubeStatus = "needs_review";
      task.youtubeError = "The publisher did not establish a resumable upload session within the safety window. Manual review is required.";
      task.updatedAt = new Date(now).toISOString();
      await env.MEDIA_BUCKET.put(object.key, JSON.stringify(task), {
        httpMetadata: { contentType: "application/json; charset=utf-8", cacheControl: "no-store" }
      });
      summary.stalePublisherJobsMarkedForReview = (summary.stalePublisherJobsMarkedForReview || 0) + 1;
      continue;
    }

    const eligibility = ageEligibleTask(task, now);
    if (!eligibility.eligible) {
      if (eligibility.reason === "resumable_upload_session_protected" || eligibility.reason === "youtube_upload_in_progress_or_retryable") {
        summary.protectedActiveUploads += 1;
      } else if (eligibility.reason === "future_publish_target_protected") {
        summary.protectedFuturePublishes += 1;
      } else if (eligibility.reason === "task_is_active_or_not_expired" || eligibility.reason === "stale_render_requires_failure_grace_period") {
        summary.skippedActiveJobs += 1;
      }
      continue;
    }

    if (dryRun) {
      if (eligibility.kind === "published") summary.publishedJobsCleaned += 1;
      else if (eligibility.kind === "expired_review") summary.reviewJobsExpired += 1;
      else if (eligibility.kind === "expired_ready") summary.abandonedReadyJobsExpired += 1;
      else summary.failedJobsExpired += 1;
      continue;
    }

    const result = await cleanupTask(
      env.MEDIA_BUCKET,
      task,
      object.key,
      eligibility.kind,
      MAX_DELETE_OBJECTS_PER_RUN - summary.deletedObjects
    );
    summary.deletedObjects += result.deleted.objects;
    summary.deletedBytes += result.deleted.bytes;
    if (!result.complete) {
      summary.cleanupLimitReached = true;
      break;
    }
    if (eligibility.kind === "published") summary.publishedJobsCleaned += 1;
    else if (eligibility.kind === "expired_review") summary.reviewJobsExpired += 1;
    else if (eligibility.kind === "expired_ready") summary.abandonedReadyJobsExpired += 1;
    else summary.failedJobsExpired += 1;
  }

  // Only remove orphan jobs if every task record was inspected successfully.
  // The task metadata itself is never deleted, preserving duplicate-upload guards.
  if (!dryRun && !summary.cleanupLimitReached && taskListing.complete) {
    const jobs = await listAll(env.MEDIA_BUCKET, "jobs/");
    if (jobs.complete) {
      const orphanKeys = jobs.objects.filter(object => {
        const jobId = uuidFromJobKey(object.key);
        const uploaded = object.uploaded ? new Date(object.uploaded).getTime() : 0;
        return jobId && !taskIds.has(jobId) && uploaded > 0 && now - uploaded >= ORPHAN_MEDIA_RETENTION_MS;
      });
      const room = Math.max(0, MAX_DELETE_OBJECTS_PER_RUN - summary.deletedObjects);
      const batch = orphanKeys.slice(0, room);
      for (let index = 0; index < batch.length; index += 25) {
        const group = batch.slice(index, index + 25);
        await Promise.all(group.map(object => env.MEDIA_BUCKET.delete(object.key)));
        summary.orphanObjectsDeleted += group.length;
        summary.deletedObjects += group.length;
        summary.deletedBytes += group.reduce((sum, object) => sum + Math.max(0, Number(object.size || 0)), 0);
      }
      if (batch.length < orphanKeys.length) summary.cleanupLimitReached = true;
    } else {
      summary.orphanScanDeferred = true;
    }
  }

  summary.deletedMiB = Number((summary.deletedBytes / (1024 * 1024)).toFixed(2));
  summary.hasMoreWork = summary.cleanupLimitReached || Boolean(summary.orphanScanDeferred);
  return summary;
}
