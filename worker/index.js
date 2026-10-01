export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return json(405, { error: "method" });
    }
    const body = new Uint8Array(await request.arrayBuffer());
    const allowed = await authorized(env.JIRA_WEBHOOK_SECRET || "", body, request.headers);
    if (!allowed) {
      return json(401, { error: "unauthorized" });
    }
    let payload;
    try {
      payload = JSON.parse(new TextDecoder().decode(body));
    } catch {
      return json(400, { error: "invalid_json" });
    }
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      return json(400, { error: "invalid_json" });
    }
    const event = payload.webhookEvent || payload.issue_event_type_name;
    if (event !== "jira:issue_created" && event !== "issue_created") {
      return json(200, { action: "ignored", reason: "event" });
    }
    const key = payload.issue && payload.issue.key;
    if (!key || typeof key !== "string") {
      return json(400, { error: "missing_key" });
    }
    const triggered = await triggerJenkins(env, key);
    if (!triggered.ok) {
      return json(500, { error: "jenkins_trigger_failed", status: triggered.status });
    }
    return json(202, { action: "queued", jiraKey: key });
  },
};

async function authorized(secret, body, headers) {
  if (!secret) return false;
  const provided = header(headers, "x-webhook-secret");
  if (provided && timingSafeEqual(provided, secret)) return true;
  for (const name of ["x-hub-signature", "x-hub-signature-256"]) {
    const signature = header(headers, name);
    if (signature && (await signatureMatches(secret, body, signature))) return true;
  }
  return false;
}

async function signatureMatches(secret, body, signature) {
  let presented = signature.trim();
  const split = presented.indexOf("=");
  if (presented.toLowerCase().startsWith("sha256=") && split !== -1) {
    presented = presented.slice(split + 1).trim();
  }
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const mac = new Uint8Array(await crypto.subtle.sign("HMAC", key, body));
  const hex = [...mac].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  return timingSafeEqual(hex, presented.toLowerCase());
}

async function triggerJenkins(env, issueKey) {
  const base = String(env.JENKINS_URL || "").replace(/\/$/, "");
  const job = env.JENKINS_JOB || "jira-redmine-sync";
  const auth = btoa(`${env.JENKINS_USER}:${env.JENKINS_API_TOKEN}`);
  const headers = { Authorization: `Basic ${auth}` };
  const crumbResponse = await fetch(`${base}/crumbIssuer/api/json`, { headers });
  if (!crumbResponse.ok) {
    return { ok: false, status: crumbResponse.status };
  }
  const crumb = await crumbResponse.json();
  const url = new URL(`${base}/job/${job}/buildWithParameters`);
  url.searchParams.set("ISSUE_KEY", issueKey);
  if (env.JENKINS_BUILD_TOKEN) url.searchParams.set("token", env.JENKINS_BUILD_TOKEN);
  const response = await fetch(url, {
    method: "POST",
    headers: {
      ...headers,
      [crumb.crumbRequestField]: crumb.crumb,
      Cookie: sessionCookie(crumbResponse),
    },
  });
  return { ok: response.status === 201 || response.status === 200 || response.status === 302, status: response.status };
}

function sessionCookie(response) {
  const cookies = typeof response.headers.getSetCookie === "function" ? response.headers.getSetCookie() : [];
  if (cookies.length) {
    return cookies.map((cookie) => cookie.split(";")[0]).join("; ");
  }
  return (response.headers.get("set-cookie") || "").split(";")[0];
}

function header(headers, name) {
  return (headers.get(name) || "").trim();
}

function timingSafeEqual(left, right) {
  if (left.length !== right.length) return false;
  let diff = 0;
  for (let index = 0; index < left.length; index += 1) {
    diff |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return diff === 0;
}

function json(status, payload) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
