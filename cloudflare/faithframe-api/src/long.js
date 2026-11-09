const MODEL = "openai/gpt-5.4-nano";
const SPACE = "https://huggingface-projects-llama-2-7b-chat.hf.space";
const SYSTEM = "You are an expert Christian long-form video scriptwriter. Respect Scripture, do not invent Bible quotations or historical facts, and return valid JSON only.";

const FORMATS = [
  {type:"cinematic_bible_story",label:"Cinematic Bible story",direction:"Retell one Bible story cinematically, using accurate biblical events, historical context, emotional narration and a practical lesson. Clearly distinguish imagined dialogue from what Scripture states."},
  {type:"bible_lesson",label:"Bible lesson and explanation",direction:"Explain one specific Bible passage carefully: its context, key ideas, misunderstandings and practical application. Never invent quotations or verse numbers."},
  {type:"guided_prayer",label:"Guided prayer and devotional",direction:"Create a thoughtful 8-12 minute guided Christian prayer for a specific real-life situation, with Scripture-grounded reflection and natural pauses. Do not promise guaranteed outcomes."},
  {type:"faith_life_lesson",label:"Faith and life lesson",direction:"Create an original relatable faith-and-life story about forgiveness, grief, temptation, hope, patience, trust, compassion or perseverance. Do not present fiction as a real testimony or Bible event."}
];

function reply(value,status) {
  return new Response(JSON.stringify(value), {
    status: status || 200,
    headers: {"content-type":"application/json; charset=utf-8","cache-control":"no-store"}
  });
}
async function bodyJson(request) {
  const raw = await request.text();
  if (raw.length > 100000) throw new Error("Request is too large.");
  return JSON.parse(raw || "{}");
}
async function readJson(bucket,key) {
  const object = await bucket.get(key);
  if (!object) return null;
  return await object.json();
}
async function writeJson(bucket,key,value) {
  await bucket.put(key,JSON.stringify(value),{httpMetadata:{contentType:"application/json; charset=utf-8",cacheControl:"no-store"}});
}
function words(value) {
  return String(value || "").trim().split(/\s+/).filter(Boolean).length;
}
function parseOutput(value) {
  let clean = String(value || "").replace(/^\uFEFF/,"").trim();
  const fence = String.fromCharCode(96).repeat(3);
  if (clean.startsWith(fence)) clean = clean.slice(3).replace(/^json\s*/i,"");
  if (clean.endsWith(fence)) clean = clean.slice(0,-3).trim();
  const first = clean.indexOf("{");
  const last = clean.lastIndexOf("}");
  if (first < 0 || last <= first) throw new Error("The long-script model did not return JSON.");
  return JSON.parse(clean.slice(first,last+1));
}
function extractText(value) {
  if (typeof value === "string") return value.trim();
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = extractText(item);
      if (found) return found;
    }
  }
  if (value && typeof value === "object") {
    const keys = ["text","response","output","content","generated_text","data","result","message"];
    for (const key of keys) {
      if (value[key] !== undefined) {
        const found = extractText(value[key]);
        if (found) return found;
      }
    }
  }
  return "";
}
async function pollinations(env,prompt) {
  if (!env.POLLINATIONS_API_KEY) throw new Error("POLLINATIONS_API_KEY is not configured.");
  const response = await fetch("https://gen.pollinations.ai/v1/chat/completions",{
    method:"POST",
    headers:{authorization:"Bearer "+env.POLLINATIONS_API_KEY,"content-type":"application/json"},
    body:JSON.stringify({
      model:MODEL,
      messages:[{role:"system",content:SYSTEM},{role:"user",content:prompt}],
      temperature:0.75,
      max_tokens:6500,
      stream:false
    }),
    signal:AbortSignal.timeout(90000)
  });
  if (!response.ok) throw new Error("Pollinations long-script generation failed (HTTP "+response.status+").");
  const data = await response.json();
  const message = data && data.choices && data.choices[0] && data.choices[0].message && data.choices[0].message.content;
  if (typeof message !== "string" || !message.trim()) throw new Error("Pollinations returned an empty long script.");
  return message.trim();
}
async function huggingFace(env,prompt) {
  if (!env.HF_TOKEN) throw new Error("HF_TOKEN is not configured for authenticated ZeroGPU fallback.");
  const headers = {authorization:"Bearer "+env.HF_TOKEN};
  const submitted = await fetch(SPACE+"/gradio_api/call/v2/generate",{
    method:"POST",
    headers:{...headers,"content-type":"application/json"},
    body:JSON.stringify({
      message:prompt,system_prompt:SYSTEM,max_new_tokens:3200,
      temperature:0.7,top_p:0.9,top_k:40,repetition_penalty:1.1
    }),
    signal:AbortSignal.timeout(30000)
  });
  if (!submitted.ok) throw new Error("Hugging Face ZeroGPU could not accept the job (HTTP "+submitted.status+").");
  const ticket = await submitted.json();
  const eventId = ticket.event_id;
  if (!eventId) throw new Error("Hugging Face ZeroGPU returned no event ID.");
  const finished = await fetch(SPACE+"/gradio_api/call/generate/"+encodeURIComponent(eventId),{
    headers:headers,signal:AbortSignal.timeout(90000)
  });
  if (!finished.ok) throw new Error("Hugging Face ZeroGPU result failed (HTTP "+finished.status+").");
  const stream = await finished.text();
  const complete = stream.split(/\r?\n\r?\n/).filter(part=>(/(^|\n)event:\s*complete\b/).test(part)).pop();
  if (!complete) throw new Error("Hugging Face ZeroGPU returned no completed result.");
  const dataLine = complete.split(/\r?\n/).find(line=>line.startsWith("data:"));
  if (!dataLine) throw new Error("Hugging Face ZeroGPU result was empty.");
  let output;
  try { output = JSON.parse(dataLine.slice(5).trim()); } catch (_) { output = dataLine.slice(5).trim(); }
  const result = extractText(output);
  if (!result) throw new Error("Hugging Face ZeroGPU returned no script text.");
  return result;
}
function validateStory(value) {
  const story = parseOutput(value);
  if (!story.title || !story.topic || !Array.isArray(story.sections) || story.sections.length < 12) {
    throw new Error("The long script must include a title, topic and 12 narration sections.");
  }
  story.sections = story.sections.slice(0,12).map((item,index)=>({
    sectionNumber:index+1,
    heading:String(item.heading || "Chapter "+(index+1)).slice(0,100),
    narration:String(item.narration || "").trim(),
    visualPrompt:String(item.visualPrompt || "").trim()
  }));
  if (story.sections.some(item=>words(item.narration)<75 || !item.visualPrompt)) {
    throw new Error("A narration section is too short or has no cinematic visual prompt.");
  }
  story.wordCount = words(story.sections.map(item=>item.narration).join(" "));
  if (story.wordCount<1050 || story.wordCount>1900) {
    throw new Error("The long script is "+story.wordCount+" words; it misses the 8-12 minute target.");
  }
  story.bibleReferences = String(story.bibleReferences || "Not applicable").slice(0,500);
  story.description = String(story.description || "").replace(/\bFaithFrame(?:\s+Studio)?\b/gi,"Gospel Inspiration").trim().slice(0,4500);
  if (!(/subscribe[\s\S]{0,140}gospel inspiration|gospel inspiration[\s\S]{0,140}subscribe/i).test(story.description)) {
    story.description += (story.description ? "\n\n" : "")+"Subscribe to Gospel Inspiration for more Bible stories, faith reflections and prayer.";
  }
  story.tags = Array.isArray(story.tags) ? story.tags.map(value=>String(value).trim()).filter(Boolean).slice(0,15) : [];
  story.thumbnailPrompt = String(story.thumbnailPrompt || story.sections[0].visualPrompt).slice(0,900);
  return story;
}
function pickFormat(history) {
  const recent = history.slice(0,2).map(item=>String(item.formatType || ""));
  const available = FORMATS.filter(item=>!recent.includes(item.type));
  const pool = available.length ? available : FORMATS;
  return pool[Math.floor(Math.random()*pool.length)];
}
async function dispatchLong(env,taskId) {
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPOSITORY) throw new Error("GitHub Actions dispatch secrets are missing.");
  const workflow = env.GITHUB_LONG_WORKFLOW || "ffmpeg-long-production-render.yml";
  const url = "https://api.github.com/repos/"+env.GITHUB_REPOSITORY+"/actions/workflows/"+encodeURIComponent(workflow)+"/dispatches";
  const response = await fetch(url,{
    method:"POST",
    headers:{
      authorization:"Bearer "+env.GITHUB_TOKEN,
      accept:"application/vnd.github+json",
      "content-type":"application/json",
      "x-github-api-version":"2022-11-28",
      "user-agent":"Gospel-Inspiration-Long-Renderer"
    },
    body:JSON.stringify({ref:env.GITHUB_REF || "main",inputs:{task_id:taskId}}),
    signal:AbortSignal.timeout(30000)
  });
  if (response.status !== 204) throw new Error("GitHub long-render workflow dispatch failed (HTTP "+response.status+").");
}
export async function produceLong(request,env) {
  let input;
  try { input = await bodyJson(request); }
  catch (error) { return reply({ok:false,error:String(error.message || error)},400); }
  const history = await readJson(env.MEDIA_BUCKET,"history/long-videos.json") || [];
  const requested = String(input.formatType || "");
  const format = FORMATS.find(item=>item.type===requested) || pickFormat(history);
  const recentTitles = history.slice(0,12).map(item=>String(item.title || item.topic || "").slice(0,100));
  const language = String(input.language || "English");
  const voiceStyle = ["female","male","auto"].includes(String(input.voiceStyle || "").toLowerCase()) ? String(input.voiceStyle).toLowerCase() : "female";
  const prompt = [
    "Create a complete original Christian YouTube long-form video for the Gospel Inspiration channel.",
    "CONTENT FORMAT: "+format.label+".",
    "CREATIVE DIRECTION: "+format.direction,
    "NICHE: Christianity. NARRATION LANGUAGE: "+language+".",
    "TARGET LENGTH: 8-12 minutes, aim for about 10 minutes. Write exactly 12 narration sections, each about 110-140 spoken words, for roughly 1,350-1,650 words total.",
    "Return valid JSON only with: title, topic, bibleReferences, description, tags (array up to 15), thumbnailPrompt and sections (exactly 12 objects containing heading, narration and visualPrompt).",
    "Each section image prompt must describe one clear 16:9 cinematic scene and preserve recurring character appearance. Narration is spoken text; do not include headings inside it.",
    "Use an engaging respectful opening, natural transitions, Scripture-grounded meaning, practical reflection and a meaningful closing. Avoid padding and repeated ideas.",
    "Never invent verses, quotations, historical facts, miracles or divine promises. Distinguish imagined dialogue from Scripture and do not present fiction as a real testimony.",
    "Visuals: historically respectful biblical environments, coherent characters, dramatic natural light, wide/medium/close shots, no text, logos, watermarks, famous actors or living-artist imitation.",
    "Do not repeat these recent topics or titles: "+(recentTitles.join(" | ") || "None recorded yet")+".",
    "Return JSON only without Markdown fences."
  ].join("\n\n");
  const failures = [];
  let story = null;
  let provider = "";
  for (const name of ["pollinations","huggingface"]) {
    try {
      const generated = name==="pollinations" ? await pollinations(env,prompt) : await huggingFace(env,prompt);
      story = validateStory(generated);
      provider = name==="pollinations" ? "pollinations" : "huggingface-zerogpu";
      break;
    } catch (error) {
      failures.push(name+": "+String(error.message || error).slice(0,400));
    }
  }
  if (!story) return reply({ok:false,error:"Both long-script providers failed. "+failures.join(" | ")},502);
  const taskId = crypto.randomUUID();
  const now = new Date().toISOString();
  const task = {
    taskId:taskId,status:"queued_media",format:"long",formatType:format.type,
    title:String(story.title).slice(0,100),topic:String(story.topic).slice(0,300),
    description:story.description,tags:story.tags,bibleReferences:story.bibleReferences,
    thumbnailPrompt:story.thumbnailPrompt,sections:story.sections,wordCount:story.wordCount,
    provider:provider,fallbackUsed:provider!=="pollinations",voiceStyle:voiceStyle,
    createdAt:now,updatedAt:now
  };
  const taskKey = "tasks/"+taskId+"/task.json";
  await writeJson(env.MEDIA_BUCKET,taskKey,task);
  await writeJson(env.MEDIA_BUCKET,"jobs/"+taskId+"/long-script.json",story);
  const updatedHistory = [{jobId:taskId,title:task.title,topic:task.topic,formatType:format.type,createdAt:now},...history].slice(0,300);
  await writeJson(env.MEDIA_BUCKET,"history/long-videos.json",updatedHistory);
  try {
    await dispatchLong(env,taskId);
    task.status="generating_media";
    task.updatedAt=new Date().toISOString();
    await writeJson(env.MEDIA_BUCKET,taskKey,task);
  } catch (error) {
    task.status="dispatch_failed";
    task.error=String(error.message || error).slice(0,500);
    await writeJson(env.MEDIA_BUCKET,taskKey,task);
    return reply({ok:false,taskId:taskId,status:task.status,error:task.error},502);
  }
  return reply({
    ok:true,taskId:taskId,status:task.status,title:task.title,topic:task.topic,
    description:task.description,tags:task.tags,bibleReferences:task.bibleReferences,
    formatType:task.formatType,wordCount:task.wordCount,sections:task.sections,
    provider:task.provider,fallbackUsed:task.fallbackUsed,voiceStyle:task.voiceStyle,
    thumbnailPrompt:task.thumbnailPrompt
  });
}
