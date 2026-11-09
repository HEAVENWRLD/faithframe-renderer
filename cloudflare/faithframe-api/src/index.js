import { produceLong } from "./long.js";
import { cleanupMedia } from "./cleanup.js";

const SHORT_SYSTEM_PROMPT = [
  "You are a thoughtful Christian cinematic YouTube Shorts writer.",
  "Create an original 30–60 second Short (about 85–135 spoken words) for a broad Christian audience.",
  "Topics may include Bible stories, Jesus, biblical characters, parables, prayer, motivation, forgiveness, hope, perseverance, Scripture reflections, church history and clearly labelled fictional stories inspired by Christian values.",
  "Treat Scripture respectfully and accurately. Never invent Bible verses, quotations, chapter references, historical facts, miracles or claims attributed to God.",
  "Distinguish direct quotation from paraphrase and include a Bible passage reference when relevant. Do not present fictional dramatizations as literal Bible events.",
  "Return plain text with these exact sections: TITLE, CATEGORY, BIBLE REFERENCE (or Not applicable), HOOK, VOICEOVER, VISUAL PLAN (4–6 numbered vertical 9:16 scenes with detailed cinematic prompts; consistent characters, no text in generated images), ON-SCREEN CAPTIONS, DESCRIPTION, HASHTAGS.",
  "The DESCRIPTION must include a warm, natural request to subscribe for more Christian stories, blessings, prayers and faith-filled videos like this one, and end with a brief God bless you message.",
  "The finished video ends with a separate 3-second subscribe card; do not claim that card is already rendered in the script.",
  "Keep visuals original and avoid copyrighted characters, famous actors or imitating a specific living artist."
].join(" ");

const POLLINATIONS_MODEL = "openai/gpt-5.4-nano";
const HF_SPACE = "https://huggingface-projects-llama-2-7b-chat.hf.space";
const IMAGE_MODEL = "@cf/black-forest-labs/flux-2-klein-4b";
const SPEECH_MODEL = "s2.1-pro-free";
const JSON_HEADERS = { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" };

function json(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: JSON_HEADERS });
}

function bearer(request) {
  const value = request.headers.get("authorization") || "";
  return value.startsWith("Bearer ") ? value.slice(7).trim() : "";
}

function authorized(request, env) {
  const routePath = new URL(request.url).pathname;
  const activePiecesRoute = routePath === "/v1/short/produce" || routePath === "/v1/long/produce" || routePath === "/v1/youtube/dispatch-ready" || routePath === "/v1/maintenance/cleanup";
  const expected = String(activePiecesRoute ? (env.ACTIVEPIECES_API_TOKEN || "") : (env.FFMPEG_WORKER_TOKEN || ""));
  const supplied = bearer(request);
  return expected.length >= 32 && supplied.length === expected.length && timingSafeEqual(supplied, expected);
}

function timingSafeEqual(a, b) {
  let result = 0;
  for (let i = 0; i < a.length; i++) result |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return result === 0;
}

async function readJson(request, maxBytes = 1_000_000) {
  const raw = await request.text();
  if (raw.length > maxBytes) throw new Error("Request body is too large.");
  try {
    const value = JSON.parse(raw);
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("JSON body must be an object.");
    return value;
  } catch (error) {
    throw new Error("Request body must contain valid JSON.");
  }
}

function safeKey(value) {
  const key = String(value || "").replace(/^\/+/, "");
  if (!key || key.length > 500 || key.includes("..") || !/^[a-zA-Z0-9/_\-.]+$/.test(key)) {
    throw new Error("Invalid asset key.");
  }
  return key;
}

async function putJson(bucket, key, value) {
  await bucket.put(key, JSON.stringify(value), {
    httpMetadata: { contentType: "application/json; charset=utf-8", cacheControl: "no-store" }
  });
}

async function getJson(bucket, key) {
  const object = await bucket.get(key);
  if (!object) return null;
  return await object.json();
}

function extractSpaceText(value) {
  if (typeof value === "string") return value.trim();
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = extractSpaceText(item);
      if (found) return found;
    }
  }
  if (value && typeof value === "object") {
    for (const key of ["text", "response", "output", "content", "generated_text"]) {
      if (typeof value[key] === "string" && value[key].trim()) return value[key].trim();
    }
    for (const key of ["data", "result", "message"]) {
      if (value[key] !== undefined) {
        const found = extractSpaceText(value[key]);
        if (found) return found;
      }
    }
  }
  return "";
}

async function generatePollinations(env, prompt) {
  if (!env.POLLINATIONS_API_KEY) throw new Error("POLLINATIONS_API_KEY is not configured on the Cloudflare Worker.");
  const response = await fetch("https://gen.pollinations.ai/v1/chat/completions", {
    method: "POST",
    headers: {
      authorization: "Bearer " + env.POLLINATIONS_API_KEY,
      "content-type": "application/json"
    },
    body: JSON.stringify({
      model: POLLINATIONS_MODEL,
      messages: [
        { role: "system", content: SHORT_SYSTEM_PROMPT },
        { role: "user", content: prompt }
      ],
      temperature: 0.7,
      max_tokens: 1200,
      stream: false
    }),
    signal: AbortSignal.timeout(45_000)
  });
  if (!response.ok) {
    const detail = (await response.text().catch(() => "")).slice(0, 500);
    throw new Error("Pollinations script call failed (HTTP " + response.status + "): " + detail);
  }
  const data = await response.json();
  const text = data && data.choices && data.choices[0] && data.choices[0].message && data.choices[0].message.content;
  if (typeof text !== "string" || !text.trim()) throw new Error("Pollinations returned an empty script.");
  return text.trim();
}

async function generateHuggingFace(env, prompt) {
  if (!env.HF_TOKEN) throw new Error("HF_TOKEN is not configured; authenticated ZeroGPU fallback is disabled.");
  const headers = { authorization: "Bearer " + env.HF_TOKEN, "content-type": "application/json" };
  const submitted = await fetch(HF_SPACE + "/gradio_api/call/v2/generate", {
    method: "POST",
    headers,
    body: JSON.stringify({
      message: prompt,
      system_prompt: SHORT_SYSTEM_PROMPT,
      max_new_tokens: 1200,
      temperature: 0.7,
      top_p: 0.9,
      top_k: 40,
      repetition_penalty: 1.1
    }),
    signal: AbortSignal.timeout(30_000)
  });
  if (!submitted.ok) throw new Error("Hugging Face ZeroGPU submit failed (HTTP " + submitted.status + ").");
  const ticket = await submitted.json();
  const eventId = ticket && ticket.event_id;
  if (!eventId || typeof eventId !== "string") throw new Error("Hugging Face ZeroGPU returned no event ID.");
  const result = await fetch(HF_SPACE + "/gradio_api/call/generate/" + encodeURIComponent(eventId), {
    headers: { authorization: "Bearer " + env.HF_TOKEN },
    signal: AbortSignal.timeout(90_000)
  });
  if (!result.ok) throw new Error("Hugging Face ZeroGPU result failed (HTTP " + result.status + ").");
  const sse = await result.text();
  const complete = sse.split(/\r?\n\r?\n/).filter(block => /(^|\n)event:\s*complete\b/.test(block)).pop();
  if (!complete) throw new Error("Hugging Face ZeroGPU did not return a completed result.");
  const dataLine = complete.split(/\r?\n/).find(line => line.startsWith("data:"));
  if (!dataLine) throw new Error("Hugging Face ZeroGPU returned an empty result.");
  let output;
  try { output = JSON.parse(dataLine.slice(5).trim()); } catch (_) { output = dataLine.slice(5).trim(); }
  const text = extractSpaceText(output);
  if (!text) throw new Error("Hugging Face ZeroGPU returned no text.");
  return text;
}

async function generateScript(request, env) {
  const input = await readJson(request, 100_000);
  const niche = String(input.niche || "Christianity").slice(0, 180);
  const topic = String(input.topic || "").slice(0, 180);
  const style = String(input.style || "cinematic");
  const language = String(input.language || "English");
  const previous = Array.isArray(input.recentIdeas) ? input.recentIdeas.slice(0, 24) : [];
  const previousText = previous.map(item => "- " + String(item).slice(0, 220)).join("\n");
  const angle = [
    "focus on an overlooked decision or turning point rather than a familiar summary",
    "use a practical modern-life situation to illuminate a specific biblical principle",
    "focus on a lesser-discussed Bible character or a small detail with a meaningful lesson",
    "build the story around forgiveness after a believable conflict",
    "explore courage while the outcome is still uncertain, without promising an easy result",
    "use a concrete act of service or compassion as the central story",
    "explore gratitude or hope during an ordinary, difficult day",
    "reflect on a short, specific Scripture passage not prominent in recent uploads",
    "tell a clearly biblical story from a fresh point of view without changing Scripture",
    "write a gentle prayer for a specific real-life situation rather than a generic prayer"
  ][Math.floor(Math.random() * 10)];
  const prompt = [
    "Create one original Christian YouTube Short for a channel about " + niche + ".",
    "Requested topic: " + (topic || "Choose a fresh topic from the full Christian niche") + ".",
    "Style: " + style + ". Language: " + language + ". Target narration: 30–60 seconds, roughly 85–135 spoken words.",
    "Include a cinematic scene plan suitable for a vertical 9:16 video.",
    "PREVIOUSLY USED IDEAS — DO NOT REPEAT their titles, main story, Bible passage, core message or narration:",
    previousText || "No previous usable scripts are supplied.",
    "Choose a substantially different topic and central idea. Keep Scripture accurate; do not invent Bible quotations or historical facts; label fictional faith-inspired stories clearly.",
    "Creative angle for this attempt: " + angle + "."
  ].join("\n\n");
  const errors = [];
  const order = input.provider === "huggingface" ? ["huggingface", "pollinations"] : ["pollinations", "huggingface"];
  for (const provider of order) {
    try {
      const script = provider === "huggingface"
        ? await generateHuggingFace(env, prompt)
        : await generatePollinations(env, prompt);
      return json({ ok: true, provider, script, requestedProvider: order[0], fallbackUsed: provider !== order[0] });
    } catch (error) {
      errors.push({ provider, error: String(error.message || error).slice(0, 500) });
    }
  }
  return json({ ok: false, errors }, 502);
}

function decodeBase64(value) {
  const normalized = String(value || "").replace(/^data:image\/[a-zA-Z0-9.+-]+;base64,/, "").replace(/\s/g, "");
  if (!normalized) throw new Error("Cloudflare returned an empty image payload.");
  const binary = atob(normalized);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  if (bytes.length < 1000) throw new Error("Cloudflare returned an unexpectedly small image.");
  return bytes;
}

async function generateImage(request, env) {
  const input = await readJson(request, 100_000);
  if (!env.CLOUDFLARE_ACCOUNT_ID || !env.CLOUDFLARE_API_TOKEN) {
    return json({ ok: false, error: "CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN must be configured." }, 503);
  }
  const prompt = String(input.prompt || "").trim();
  if (prompt.length < 20 || prompt.length > 5000) return json({ error: "Image prompt must contain 20–5000 characters." }, 400);
  const width = Math.max(256, Math.min(1280, Number(input.width || 576)));
  const height = Math.max(256, Math.min(1280, Number(input.height || 1024)));
  const form = new FormData();
  form.append("prompt", prompt);
  form.append("width", String(width));
  form.append("height", String(height));
  const response = await fetch(
    "https://api.cloudflare.com/client/v4/accounts/" + encodeURIComponent(env.CLOUDFLARE_ACCOUNT_ID) + "/ai/run/" + IMAGE_MODEL,
    {
      method: "POST",
      headers: { authorization: "Bearer " + env.CLOUDFLARE_API_TOKEN },
      body: form,
      signal: AbortSignal.timeout(120_000)
    }
  );
  if (!response.ok) return json({ ok: false, error: "Cloudflare FLUX.2 Klein 4B failed (HTTP " + response.status + ")." }, 502);
  const data = await response.json();
  if (!data || !data.success || !data.result || typeof data.result.image !== "string") {
    return json({ ok: false, error: "Cloudflare FLUX.2 Klein 4B did not return an image.", details: data && data.errors || [] }, 502);
  }
  const bytes = decodeBase64(data.result.image);
  const jobId = String(input.jobId || crypto.randomUUID()).replace(/[^a-zA-Z0-9_-]/g, "").slice(0, 80);
  const fileName = String(input.fileName || "scene-01").replace(/[^a-zA-Z0-9_-]/g, "").slice(0, 80);
  const key = safeKey("jobs/" + jobId + "/images/" + fileName + ".png");
  await env.MEDIA_BUCKET.put(key, bytes, { httpMetadata: { contentType: "image/png", cacheControl: "private, max-age=0" } });
  return json({ ok: true, assetKey: key, contentType: "image/png", sizeBytes: bytes.byteLength, width, height });
}

async function generateNarration(request, env) {
  const input = await readJson(request, 100_000);
  if (!env.FISH_API_KEY) return json({ ok: false, error: "FISH_API_KEY is not configured." }, 503);
  const value = String(input.text || "").trim();
  if (value.length < 20 || value.length > 5000) return json({ error: "Narration text must contain 20–5000 characters." }, 400);
  const voiceStyle = String(input.voiceStyle || "female").toLowerCase();
  const direction = voiceStyle === "male"
    ? "[warm] [reassuring] [steady, deep narrator] "
    : voiceStyle === "auto"
      ? "[warm] [sincere] [gentle storyteller] "
      : "[warm] [reassuring] [gentle narrator] ";
  const response = await fetch("https://api.fish.audio/v1/tts", {
    method: "POST",
    headers: {
      authorization: "Bearer " + env.FISH_API_KEY,
      "content-type": "application/json",
      model: SPEECH_MODEL
    },
    body: JSON.stringify({ text: direction + value, format: "mp3" }),
    signal: AbortSignal.timeout(120_000)
  });
  if (!response.ok) return json({ ok: false, error: "Fish Audio " + SPEECH_MODEL + " failed (HTTP " + response.status + ")." }, 502);
  const bytes = await response.arrayBuffer();
  if (bytes.byteLength < 1000) return json({ ok: false, error: "Fish Audio returned an unexpectedly small audio file." }, 502);
  const jobId = String(input.jobId || crypto.randomUUID()).replace(/[^a-zA-Z0-9_-]/g, "").slice(0, 80);
  const fileName = String(input.fileName || "narration").replace(/[^a-zA-Z0-9_-]/g, "").slice(0, 80);
  const key = safeKey("jobs/" + jobId + "/audio/" + fileName + ".mp3");
  await env.MEDIA_BUCKET.put(key, bytes, { httpMetadata: { contentType: "audio/mpeg", cacheControl: "private, max-age=0" } });
  return json({ ok: true, assetKey: key, contentType: "audio/mpeg", sizeBytes: bytes.byteLength, model: SPEECH_MODEL });
}

function scheduledEATIso(hour) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Africa/Nairobi",
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  }).formatToParts(new Date());
  const values = {};
  for (const part of parts) if (part.type !== "literal") values[part.type] = Number(part.value);
  // Kenya remains UTC+3; 20:00 EAT is 17:00 UTC and 22:00 EAT is 19:00 UTC.
  return new Date(Date.UTC(values.year, values.month - 1, values.day, Number(hour) - 3, 0, 0)).toISOString();
}

async function githubDispatch(env, workflow, taskId) {
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPOSITORY) throw new Error("GitHub Actions dispatch secrets are not configured.");
  const file = workflow || env.GITHUB_SHORT_WORKFLOW || "ffmpeg-short-production-render.yml";
  const url = "https://api.github.com/repos/" + env.GITHUB_REPOSITORY + "/actions/workflows/" + encodeURIComponent(file) + "/dispatches";
  const response = await fetch(url, {
    method: "POST",
    headers: {
      authorization: "Bearer " + env.GITHUB_TOKEN,
      accept: "application/vnd.github+json",
      "content-type": "application/json",
      "x-github-api-version": "2022-11-28",
      "user-agent": "Gospel-Inspiration-Renderer"
    },
    body: JSON.stringify({ ref: env.GITHUB_REF || "main", inputs: { task_id: taskId } }),
    signal: AbortSignal.timeout(30_000)
  });
  if (response.status !== 204) {
    const detail = (await response.text().catch(() => "")).slice(0, 500);
    throw new Error("GitHub Actions dispatch failed (HTTP " + response.status + "): " + detail);
  }
}

async function createRenderTask(request, env) {
  const input = await readJson(request, 250_000);
  const taskId = String(input.taskId || crypto.randomUUID());
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(taskId)) {
    return json({ error: "taskId must be a UUID." }, 400);
  }
  if (!Array.isArray(input.sceneAssets) || input.sceneAssets.length < 4 || input.sceneAssets.length > 30) {
    return json({ error: "A Short render task needs 4–30 scene assets." }, 400);
  }
  const sceneAssets = input.sceneAssets.map((asset, index) => ({
    sceneNumber: Number(asset.sceneNumber || index + 1),
    assetKey: safeKey(asset.assetKey),
    contentType: String(asset.contentType || "image/png")
  })).sort((a, b) => a.sceneNumber - b.sceneNumber);
  const narration = input.narration && { assetKey: safeKey(input.narration.assetKey), contentType: "audio/mpeg" };
  if (!narration) return json({ error: "A narration asset is required." }, 400);
  const captionSrt = String(input.captionSrt || "");
  const captionText = String(input.captionText || "");
  if (captionSrt.length < 10 && captionText.length < 20) return json({ error: "Timed captions or caption text are required." }, 400);
  const task = {
    taskId,
    status: "queued",
    format: "short",
    targetPublishAt: String(input.targetPublishAt || scheduledEATIso(20)),
    youtubeStatus: "pending",
    title: String(input.title || "A Message of Faith").slice(0, 180),
    description: String(input.description || "").slice(0, 5000),
    tags: Array.isArray(input.tags) ? input.tags.map(value => String(value).trim()).filter(Boolean).slice(0, 15) : [],
    thumbnailAsset: input.thumbnailAsset && input.thumbnailAsset.assetKey
      ? { assetKey: safeKey(input.thumbnailAsset.assetKey), contentType: "image/png" } : null,
    sceneAssets,
    narration,
    music: input.music && input.music.assetKey ? { assetKey: safeKey(input.music.assetKey), contentType: "audio/wav" } : null,
    outro: input.outro && input.outro.assetKey ? { assetKey: safeKey(input.outro.assetKey), contentType: String(input.outro.contentType || "image/svg+xml") } : null,
    captionSrt,
    captionText,
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString()
  };
  const taskKey = "tasks/" + taskId + "/task.json";
  const existing = await env.MEDIA_BUCKET.head(taskKey);
  if (existing) return json({ error: "A render task with this ID already exists." }, 409);
  await putJson(env.MEDIA_BUCKET, taskKey, task);
  try {
    await githubDispatch(env, env.GITHUB_SHORT_WORKFLOW, taskId);
    task.status = "rendering";
    task.updatedAt = new Date().toISOString();
    await putJson(env.MEDIA_BUCKET, taskKey, task);
  } catch (error) {
    task.status = "dispatch_failed";
    task.error = String(error.message || error).slice(0, 600);
    task.updatedAt = new Date().toISOString();
    await putJson(env.MEDIA_BUCKET, taskKey, task);
    return json({ ok: false, taskId, status: task.status, error: task.error }, 502);
  }
  return json({ ok: true, taskId, status: task.status });
}


function headingRemainder(line, heading) {
  let value = String(line || "").trim().replace(/^#{1,3}\s*/, "");
  const target = String(heading || "").toLowerCase();
  if (!value.toLowerCase().startsWith(target)) return null;
  let rest = value.slice(target.length);
  if (rest && !/^\s*(?:\([^)]*\))?\s*:/.test(rest)) return null;
  rest = rest.replace(/^\s*\([^)]*\)\s*/, "").replace(/^\s*:\s*/, "").trim();
  return rest;
}

function extractSection(script, heading, stopHeadings) {
  const lines = String(script || "").split(/\r?\n/);
  const output = [];
  let collecting = false;
  for (const line of lines) {
    if (!collecting) {
      const value = headingRemainder(line, heading);
      if (value !== null) {
        collecting = true;
        if (value) output.push(value);
      }
      continue;
    }
    let stop = false;
    for (const name of stopHeadings) {
      if (headingRemainder(line, name) !== null) {
        stop = true;
        break;
      }
    }
    if (stop) break;
    output.push(line);
  }
  return output.join("\n").trim();
}

function extractShortFields(script) {
  const titleMatch = String(script).match(/(?:^|\n)\s*(?:#{1,3}\s*)?TITLE\s*:?\s*([^\n]+)/i);
  const title = titleMatch ? titleMatch[1].replace(/^[\s"'*]+|[\s"'*]+$/g, "").slice(0, 180) : "A Message of Faith";
  const voiceover = extractSection(script, "VOICEOVER", ["VISUAL PLAN", "ON-SCREEN CAPTIONS", "CAPTIONS", "DESCRIPTION", "HASHTAGS"]);
  if (voiceover.length < 80) throw new Error("Generated Short did not contain a usable VOICEOVER section.");
  const visualPlan = extractSection(script, "VISUAL PLAN", ["ON-SCREEN CAPTIONS", "CAPTIONS", "DESCRIPTION", "HASHTAGS"]);
  const scenes = [];
  let current = null;
  for (const line of visualPlan.split(/\r?\n/)) {
    const match = line.match(/^\s*(?:[-*]\s*)?(?:scene\s*)?(\d{1,2})\s*[).:\-\u2013\u2014]\s*(.+?)\s*$/i);
    if (match) {
      const sceneNumber = Number(match[1]);
      const prompt = String(match[2] || "").trim();
      if (sceneNumber >= 1 && sceneNumber <= 10 && prompt.length >= 20 &&
          !scenes.some(item => item.sceneNumber === sceneNumber)) {
        current = { sceneNumber, prompt };
        scenes.push(current);
      } else {
        current = null;
      }
    } else if (current && line.trim()) {
      current.prompt += " " + line.trim();
    }
  }
  const ordered = scenes.sort((a, b) => a.sceneNumber - b.sceneNumber).slice(0, 6);
  if (ordered.length < 4) throw new Error("Generated Short must contain at least four numbered visual scenes.");
  const description = extractSection(script, "DESCRIPTION", ["HASHTAGS"]).slice(0, 4000);
  const hashtagText = extractSection(script, "HASHTAGS", []);
  const tags = Array.from(hashtagText.matchAll(/#?([A-Za-z0-9][A-Za-z0-9_-]{1,39})/g))
    .map(match => match[1])
    .filter((value, index, values) => values.findIndex(item => item.toLowerCase() === value.toLowerCase()) === index)
    .slice(0, 15);
  const bibleReference = extractSection(script, "BIBLE REFERENCE", ["HOOK", "VOICEOVER", "VISUAL PLAN"]).split(/\r?\n/)[0].slice(0, 160);
  const category = extractSection(script, "CATEGORY", ["BIBLE REFERENCE", "HOOK", "VOICEOVER"]).split(/\r?\n/)[0].slice(0, 100);
  return { title, voiceover, scenes: ordered, description, tags, bibleReference, category };
}

async function internalJsonCall(handler, env, payload) {
  const request = new Request("https://internal.gospel-inspiration/v1", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload)
  });
  const response = await handler(request, env);
  const result = await response.json();
  if (!response.ok || result?.ok === false || result?.error) {
    throw new Error(String(result?.error || "A media provider request failed.").slice(0, 600));
  }
  return result;
}

async function produceShort(request, env) {
  const input = await readJson(request, 100_000);
  const jobId = crypto.randomUUID();
  const voiceStyle = ["female", "male", "auto"].includes(String(input.voiceStyle || "").toLowerCase())
    ? String(input.voiceStyle).toLowerCase() : "female";
  const provider = input.provider === "huggingface" ? "huggingface" : "pollinations";
  const scriptResult = await internalJsonCall(generateScript, env, {
    niche: String(input.niche || "Christianity").slice(0, 180),
    topic: String(input.topic || "").slice(0, 180),
    style: String(input.style || "cinematic"),
    language: String(input.language || "English"),
    provider,
    recentIdeas: Array.isArray(input.recentIdeas) ? input.recentIdeas.slice(0, 24) : []
  });
  const fields = extractShortFields(scriptResult.script);
  const sceneAssets = await Promise.all(fields.scenes.map(async (scene) => {
    const prompt = [
      "Original vertical 9:16 cinematic Christian YouTube Shorts scene, 576x1024.",
      "Premium cinematic realism, clear focal subject, natural dramatic light, reverent hopeful mood.",
      "No text, letters, symbols, captions, typography, logos, watermarks or borders.",
      "Keep recurring character appearance, age, clothing and color palette consistent across the scenes.",
      scene.prompt
    ].join(" ");
    const saved = await internalJsonCall(generateImage, env, {
      jobId,
      fileName: "scene-" + String(scene.sceneNumber).padStart(2, "0"),
      prompt,
      width: 576,
      height: 1024
    });
    return {
      sceneNumber: scene.sceneNumber,
      assetKey: saved.assetKey,
      contentType: saved.contentType || "image/png",
      prompt: scene.prompt
    };
  }));
  const narration = await internalJsonCall(generateNarration, env, {
    jobId,
    text: fields.voiceover,
    voiceStyle
  });
  const thumbnail = await internalJsonCall(generateImage, env, {
    jobId,
    fileName: "short-thumbnail",
    prompt: [
      "Premium 16:9 landscape YouTube thumbnail, 1280x720, for a Christian Gospel Inspiration Short.",
      "Cinematic, emotionally clear, polished dramatic lighting, powerful single focal subject, readable composition at small size.",
      "No text, letters, typography, logos, watermark or border.",
      "Do not depict an event as biblical fact unless supported by Scripture.",
      "Title/theme: " + fields.title + ".",
      "Visual direction: " + fields.scenes[0].prompt + "."
    ].join(" "),
    width: 1280,
    height: 720
  });
  const taskResult = await internalJsonCall(createRenderTask, env, {
    taskId: jobId,
    title: fields.title,
    description: fields.description,
    tags: fields.tags,
    thumbnailAsset: { assetKey: thumbnail.assetKey, contentType: "image/png" },
    sceneAssets,
    narration: { assetKey: narration.assetKey, contentType: "audio/mpeg" },
    captionText: fields.voiceover,
    targetPublishAt: scheduledEATIso(20)
  });
  await putJson(env.MEDIA_BUCKET, "jobs/" + jobId + "/production.json", {
    jobId,
    status: taskResult.status || "rendering",
    title: fields.title,
    script: scriptResult.script,
    provider: scriptResult.provider,
    fallbackUsed: scriptResult.fallbackUsed,
    voiceStyle,
    bibleReference: fields.bibleReference,
    category: fields.category,
    description: fields.description,
    scenePrompts: fields.scenes,
    sceneAssets,
    narrationAssetKey: narration.assetKey,
    taskId: taskResult.taskId,
    createdAt: new Date().toISOString()
  });
  return json({
    ok: true,
    jobId,
    taskId: taskResult.taskId,
    status: taskResult.status,
    title: fields.title,
    description: fields.description,
    tags: fields.tags,
    thumbnailAssetKey: thumbnail.assetKey,
    script: scriptResult.script,
    provider: scriptResult.provider,
    fallbackUsed: scriptResult.fallbackUsed,
    voiceStyle,
    bibleReference: fields.bibleReference,
    category: fields.category,
    sceneCount: sceneAssets.length,
    sceneAssets,
    narrationAssetKey: narration.assetKey
  });
}

async function dispatchReadyYoutube(env) {
  const now = Date.now();
  const dispatched = [];
  const skipped = [];
  let cursor;
  let pages = 0;
  do {
    const listing = await env.MEDIA_BUCKET.list({ prefix: "tasks/", cursor, limit: 1000 });
    for (const object of (listing.objects || [])) {
      if (!object.key.endsWith("/task.json")) continue;
      const task = await getJson(env.MEDIA_BUCKET, object.key);
      if (!task || task.status !== "ready") continue;
      if (!task.targetPublishAt || !Number.isFinite(Date.parse(task.targetPublishAt)) || Date.parse(task.targetPublishAt) > now) continue;
      const publishState = String(task.youtubeStatus || "pending");
      if (!["pending", "upload_retryable"].includes(publishState)) continue;
      const taskId = String(task.taskId || "");
      if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(taskId)) {
        skipped.push({ taskId: taskId.slice(0, 40), reason: "invalid_task_id" });
        continue;
      }
      const attempts = Number(task.youtubeUploadAttempts || 0);
      if (attempts >= 3) {
        task.youtubeStatus = "needs_review";
        task.youtubeError = "Automatic YouTube upload reached its three-attempt safety limit.";
        task.updatedAt = new Date().toISOString();
        await putJson(env.MEDIA_BUCKET, object.key, task);
        skipped.push({ taskId, reason: "attempt_limit_needs_review" });
        continue;
      }
      task.youtubeStatus = "upload_queued";
      task.youtubeUploadAttempts = attempts + 1;
      task.youtubeDispatchAt = new Date().toISOString();
      task.updatedAt = new Date().toISOString();
      await putJson(env.MEDIA_BUCKET, object.key, task);
      try {
        await githubDispatch(env, env.GITHUB_YOUTUBE_WORKFLOW || "ffmpeg-youtube-publish.yml", taskId);
        dispatched.push({ taskId, format: task.format || "unknown", attempt: task.youtubeUploadAttempts });
      } catch (error) {
        // A lost response could mean GitHub accepted the dispatch. Do not
        // automatically send a second dispatch and risk a duplicate upload.
        task.youtubeStatus = "needs_review";
        task.youtubeError = "Could not confirm GitHub publisher dispatch: " + String(error.message || error).slice(0, 350);
        task.updatedAt = new Date().toISOString();
        await putJson(env.MEDIA_BUCKET, object.key, task);
        skipped.push({ taskId, reason: "publisher_dispatch_needs_review" });
      }
    }
    cursor = listing.truncated ? listing.cursor : undefined;
    pages += 1;
  } while (cursor && pages < 20);
  return json({
    ok: true,
    dispatchedCount: dispatched.length,
    dispatched,
    skippedCount: skipped.length,
    skipped,
    morePagesRemaining: Boolean(cursor),
    checkedAt: new Date(now).toISOString()
  });
}

async function route(request, env) {
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/+$/, "") || "/";
  if (request.method === "GET" && path === "/health") return json({ ok: true, service: "gospel-inspiration-media", version: 1 });
  if (!authorized(request, env)) return json({ error: "Unauthorized." }, 401);
  if (request.method === "POST" && path === "/v1/script") return await generateScript(request, env);
  if (request.method === "POST" && path === "/v1/short/produce") return await produceShort(request, env);
  if (request.method === "POST" && path === "/v1/long/produce") return await produceLong(request, env);
  if (request.method === "POST" && path === "/v1/youtube/dispatch-ready") return await dispatchReadyYoutube(env);
  if (request.method === "POST" && path === "/v1/maintenance/cleanup") {
    const input = await readJson(request, 10_000);
    return json(await cleanupMedia(env, { dryRun: input.dryRun === true }));
  }
  if (request.method === "POST" && path === "/v1/image") return await generateImage(request, env);
  if (request.method === "POST" && path === "/v1/narration") return await generateNarration(request, env);
  if (request.method === "POST" && path === "/v1/tasks") return await createRenderTask(request, env);

  const assetMatch = path.match(/^\/v1\/assets\/(.+)$/);
  if (request.method === "GET" && assetMatch) {
    const key = safeKey(decodeURIComponent(assetMatch[1]));
    const object = await env.MEDIA_BUCKET.get(key);
    if (!object) return json({ error: "Asset not found." }, 404);
    const headers = new Headers();
    object.writeHttpMetadata(headers);
    headers.set("cache-control", "private, no-store");
    headers.set("etag", object.httpEtag);
    return new Response(object.body, { headers });
  }

  const taskMatch = path.match(/^\/v1\/tasks\/([0-9a-f-]{36})$/i);
  if (request.method === "GET" && taskMatch) {
    const task = await getJson(env.MEDIA_BUCKET, "tasks/" + taskMatch[1] + "/task.json");
    return task ? json(task) : json({ error: "Task not found." }, 404);
  }

  const thumbnailMatch = path.match(/^\/v1\/tasks\/([0-9a-f-]{36})\/thumbnail$/i);
  if (thumbnailMatch) {
    const taskId = thumbnailMatch[1];
    const taskKey = "tasks/" + taskId + "/task.json";
    const task = await getJson(env.MEDIA_BUCKET, taskKey);
    if (!task) return json({ error: "Task not found." }, 404);
    if (request.method === "PUT") {
      const body = await request.arrayBuffer();
      if (body.byteLength < 1000 || body.byteLength > 12 * 1024 * 1024) return json({ error: "Thumbnail size is invalid." }, 400);
      const signature = new Uint8Array(body.slice(0, 8));
      if (signature[0] !== 0x89 || signature[1] !== 0x50 || signature[2] !== 0x4e || signature[3] !== 0x47) return json({ error: "Thumbnail must be a PNG image." }, 400);
      const key = "tasks/" + taskId + "/thumbnail.png";
      await env.MEDIA_BUCKET.put(key, body, { httpMetadata: { contentType: "image/png", cacheControl: "private, no-store" } });
      task.thumbnailKey = key;
      task.updatedAt = new Date().toISOString();
      await putJson(env.MEDIA_BUCKET, taskKey, task);
      return json({ ok: true, taskId: taskId, thumbnailKey: key, sizeBytes: body.byteLength });
    }
    if (request.method === "GET") {
      if (!task.thumbnailKey) return json({ error: "Thumbnail is not ready." }, 404);
      const object = await env.MEDIA_BUCKET.get(task.thumbnailKey);
      if (!object) return json({ error: "Thumbnail object is missing." }, 404);
      const headers = new Headers({ "content-type": "image/png", "cache-control": "private, no-store" });
      headers.set("content-length", String(object.size));
      return new Response(object.body, { headers });
    }
  }

  const resultMatch = path.match(/^\/v1\/tasks\/([0-9a-f-]{36})\/result$/i);
  if (resultMatch) {
    const taskId = resultMatch[1];
    const taskKey = "tasks/" + taskId + "/task.json";
    const task = await getJson(env.MEDIA_BUCKET, taskKey);
    if (!task) return json({ error: "Task not found." }, 404);
    if (request.method === "PUT") {
      const contentLength = Number(request.headers.get("content-length") || 0);
      if (contentLength > 100 * 1024 * 1024) return json({ error: "Rendered video exceeds the 100 MB worker upload limit." }, 413);
      const body = await request.arrayBuffer();
      if (body.byteLength < 50_000) return json({ error: "Rendered MP4 is unexpectedly small." }, 400);
      const signature = new Uint8Array(body.slice(4, 8));
      if (String.fromCharCode(...signature) !== "ftyp") return json({ error: "Uploaded result is not an MP4 file." }, 400);
      const key = "tasks/" + taskId + "/rendered.mp4";
      await env.MEDIA_BUCKET.put(key, body, { httpMetadata: { contentType: "video/mp4", cacheControl: "private, no-store" } });
      task.status = "ready";
      task.resultKey = key;
      task.resultSizeBytes = body.byteLength;
      task.updatedAt = new Date().toISOString();
      await putJson(env.MEDIA_BUCKET, taskKey, task);
      return json({ ok: true, taskId, status: task.status, resultKey: key, sizeBytes: body.byteLength });
    }
    if (request.method === "GET") {
      if (task.status !== "ready" || !task.resultKey) return json({ error: "Rendered result is not ready.", status: task.status }, 409);
      const object = await env.MEDIA_BUCKET.get(task.resultKey);
      if (!object) return json({ error: "Rendered result is missing from storage." }, 404);
      const headers = new Headers({ "content-type": "video/mp4", "cache-control": "private, no-store" });
      headers.set("content-length", String(object.size));
      return new Response(object.body, { headers });
    }
  }

  const youtubeSessionMatch = path.match(/^\/v1\/tasks\/([0-9a-f-]{36})\/youtube-session$/i);
  if (youtubeSessionMatch && request.method === "PUT") {
    const taskId = youtubeSessionMatch[1];
    const key = "tasks/" + taskId + "/task.json";
    const task = await getJson(env.MEDIA_BUCKET, key);
    if (!task) return json({ error: "Task not found." }, 404);
    if (task.youtubeVideoId) return json({ error: "This task already has a completed YouTube video ID." }, 409);
    const input = await readJson(request, 10_000);
    const sessionUrl = String(input.sessionUrl || "");
    let parsed;
    try { parsed = new URL(sessionUrl); } catch (_) { return json({ error: "A valid YouTube resumable session URL is required." }, 400); }
    if (parsed.protocol !== "https:" || parsed.hostname !== "www.googleapis.com" || parsed.pathname !== "/upload/youtube/v3/videos") {
      return json({ error: "The upload session URL is not a valid YouTube resumable endpoint." }, 400);
    }
    const totalBytes = Number(input.totalBytes || 0);
    if (!Number.isSafeInteger(totalBytes) || totalBytes < 50_000 || totalBytes > 100 * 1024 * 1024) {
      return json({ error: "The video size is outside the allowed upload range." }, 400);
    }
    task.youtubeUploadSessionUrl = sessionUrl;
    task.youtubeTotalBytes = totalBytes;
    task.youtubeStatus = "uploading";
    task.updatedAt = new Date().toISOString();
    await putJson(env.MEDIA_BUCKET, key, task);
    return json({ ok: true, taskId, youtubeStatus: task.youtubeStatus });
  }

  const youtubeResultMatch = path.match(/^\/v1\/tasks\/([0-9a-f-]{36})\/youtube-result$/i);
  if (youtubeResultMatch && request.method === "PUT") {
    const taskId = youtubeResultMatch[1];
    const key = "tasks/" + taskId + "/task.json";
    const task = await getJson(env.MEDIA_BUCKET, key);
    if (!task) return json({ error: "Task not found." }, 404);
    const input = await readJson(request, 20_000);
    const videoId = String(input.videoId || "");
    if (!/^[A-Za-z0-9_-]{6,32}$/.test(videoId)) return json({ error: "YouTube returned an invalid video ID." }, 400);
    const privacy = String(input.privacyStatus || "");
    if (!["public", "unlisted", "private"].includes(privacy)) return json({ error: "The verified YouTube privacy status is invalid." }, 400);
    task.youtubeVideoId = videoId;
    task.youtubeUrl = "https://www.youtube.com/watch?v=" + encodeURIComponent(videoId);
    task.youtubePrivacyStatus = privacy;
    task.youtubeMadeForKids = input.madeForKids === true;
    task.youtubeStatus = privacy === "public" && input.madeForKids === false ? "published" : "uploaded_private";
    task.youtubeThumbnailApplied = input.thumbnailApplied === true;
    task.youtubePublishedAt = new Date().toISOString();
    task.youtubeUploadSessionUrl = task.youtubeUploadSessionUrl || null;
    task.youtubeError = null;
    task.updatedAt = new Date().toISOString();
    await putJson(env.MEDIA_BUCKET, key, task);
    return json({ ok: true, taskId, videoId, videoUrl: task.youtubeUrl, youtubeStatus: task.youtubeStatus, privacyStatus: privacy });
  }

  const youtubeFailureMatch = path.match(/^\/v1\/tasks\/([0-9a-f-]{36})\/youtube-failure$/i);
  if (youtubeFailureMatch && request.method === "POST") {
    const taskId = youtubeFailureMatch[1];
    const key = "tasks/" + taskId + "/task.json";
    const task = await getJson(env.MEDIA_BUCKET, key);
    if (!task) return json({ error: "Task not found." }, 404);
    if (task.youtubeVideoId) return json({ error: "A completed YouTube video is already recorded." }, 409);
    const input = await readJson(request, 10_000);
    const message = String(input.message || "YouTube upload requires review.").slice(0, 700);
    const transient = /HTTP (?:429|500|502|503|504)|transient|network interruption|could not reach|status check failed transiently/i.test(message);
    const attempts = Number(task.youtubeUploadAttempts || 0);
    task.youtubeStatus = transient && attempts < 3 ? "upload_retryable" : "needs_review";
    task.youtubeError = message;
    task.updatedAt = new Date().toISOString();
    await putJson(env.MEDIA_BUCKET, key, task);
    return json({ ok: true, taskId, youtubeStatus: task.youtubeStatus });
  }

  const failMatch = path.match(/^\/v1\/tasks\/([0-9a-f-]{36})\/failure$/i);
  if (request.method === "POST" && failMatch) {
    const taskId = failMatch[1];
    const key = "tasks/" + taskId + "/task.json";
    const task = await getJson(env.MEDIA_BUCKET, key);
    if (!task) return json({ error: "Task not found." }, 404);
    const body = await readJson(request, 10_000);
    task.status = "failed";
    task.error = String(body.message || "Renderer failed.").slice(0, 600);
    task.updatedAt = new Date().toISOString();
    await putJson(env.MEDIA_BUCKET, key, task);
    return json({ ok: true, taskId, status: task.status });
  }

  return json({ error: "Not found." }, 404);
}

export default {
  async fetch(request, env) {
    try {
      return await route(request, env);
    } catch (error) {
      return json({ error: String(error.message || error).slice(0, 700) }, 500);
    }
  }
};
