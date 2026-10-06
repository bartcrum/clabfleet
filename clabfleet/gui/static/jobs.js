"use strict";

// ---------------------------------------------------------------------------
// The Activity pane: a job's output, followed while it runs, and its steps
// (B5): from the lines a deploy prints, each host's way through
// plan → images → deploy → links → ready. Durations come from when lines
// arrived, so only for a job followed while it ran.
//
// Uses app.js globals: S, $, h, api, fmtDuration, naturalCmp,
// renderJobPicker, renderJobMeta, renderJobCancel, JOB_ENDED, jobDone, announce, refreshState,
// reloadDetail, refreshHosts; and from dock.js: activatePane.
// ---------------------------------------------------------------------------

const STEPS = ["plan", "images", "deploy", "links", "ready"];
const LAB_STEPS = new Set(["plan", "images"]);
const STEP_OF = [
  [/Probing \d+ cluster hosts|Computing placement|Using VNIs|Checking VXLAN|Lab '.*' is on/, "plan"],
  [/Pulling \S+ on |images? (missing|check)/i, "images"],
  [/Deploying '.*' locally|Copying '.*' \(|Running containerlab deploy|Creating lab directory|Creating container|Creating docker network/, "deploy"],
  [/Created link|Creating virtual wire|vxlan/i, "links"],
  [/Running postdeploy|Waiting up to|Adding host entries|Adding SSH config/, "ready"],
];

function newSteps(job) {
  return { action: job?.action, rows: new Map(), lastHost: null, live: false, status: job?.status };
}

function stepRow(st, host) {
  if (!st.rows.has(host)) st.rows.set(host, Object.fromEntries(STEPS.map((k) => [k, { state: "pending" }])));
  return st.rows.get(host);
}

// Mark ``step`` active on ``host`` (and the earlier steps done)
function enterStep(st, host, step, t) {
  const row = stepRow(st, host);
  const idx = STEPS.indexOf(step);
  STEPS.forEach((k, i) => {
    const s0 = row[k];
    if (i < idx && s0.state !== "done") { s0.state = "done"; s0.end ??= t; s0.start ??= t; }
  });
  const cur = row[step];
  if (cur.state === "pending") { cur.state = "active"; cur.start = t; }
}

function stepLine(st, line, t) {
  if (!["deploy", "redeploy"].includes(st.action)) return;
  const m = /^\[([^\]]+)\] (.*)$/.exec(line);
  let host = m ? m[1] : null;
  const text = m ? m[2] : line;
  const named = / on ([\w.-]+?)(?:[ ,:]|$)| to ([\w.-]+):/.exec(text);
  if (!host && named) host = named[1] || named[2];
  for (const [re, step] of STEP_OF) {
    if (!re.test(text)) continue;
    if (LAB_STEPS.has(step)) enterStep(st, "lab", step, t);
    else enterStep(st, host || st.lastHost || "this host", step, t);
    break;
  }
  if (host) st.lastHost = host;
}

function finishSteps(st, status, t) {
  st.status = status;
  for (const row of st.rows.values()) {
    for (const k of STEPS) {
      const s0 = row[k];
      if (s0.state === "active") { s0.state = status === "ok" ? "done" : "failed"; s0.end = t; }
    }
  }
}

function renderSteps(job) {
  const box = $("#job-steps");
  const st = S.steps;
  if (!st || !st.rows.size) { box.hidden = true; return; }
  const now = Date.now() / 1000;
  const fmt = (s0) => (st.live && s0.start != null ? fmtDuration((s0.end ?? now) - s0.start) : "");
  // One host: lines carry no host name, the job's times do
  const timed = Object.keys(job?.host_times || (S.state?.jobs || []).find((j) => j.id === S.viewJob)?.host_times || {});
  if (st.rows.has("this host") && timed.length === 1 && !st.rows.has(timed[0])) {
    st.rows.set(timed[0], st.rows.get("this host"));
    st.rows.delete("this host");
  }
  job = job || (S.state?.jobs || []).find((j) => j.id === S.viewJob);
  const rows = [...st.rows].sort(([a], [b]) => (a === "lab" ? -1 : b === "lab" ? 1 : naturalCmp(a, b)));
  box.replaceChildren(...rows.map(([host, row]) => h("div", { class: "step-row" },
    h("span", { class: "step-host mono", title: host === "lab" ? "Steps for the whole lab" : host },
      host === "lab" ? "lab" : host),
    ...STEPS.filter((k) => (host === "lab") === LAB_STEPS.has(k)).map((k) => h("span", {
      class: `step ${row[k].state}`, title: `${k}: ${row[k].state}`,
    }, h("span", { class: "step-dot", "aria-hidden": "true" }), k, h("span", { class: "muted" }, fmt(row[k])))),
    host !== "lab" && !st.live && job?.host_times?.[host] != null
      ? h("span", { class: "muted small" }, `${fmtDuration(job.host_times[host])} in all`) : null)));
  box.hidden = false;
}

// Show a job's output in the Activity pane, following it while it runs
function trackJob(id, fromStart) {
  clearTimeout(S.jobPolling);
  S.jobPolling = null;
  S.viewJob = id;
  const pre = $("#activity");
  pre.dataset.fresh = "";
  pre.textContent = "";
  let offset = 0;
  let sawRunning = false;
  S.steps = newSteps((S.state?.jobs || []).find((j) => j.id === id));
  renderSteps();
  if (fromStart) activatePane("activity", true);
  renderJobPicker();

  const poll = async () => {
    let job;
    try {
      job = await api(`/api/jobs/${id}?offset=${offset}`);
    } catch (e) {
      if (S.viewJob === id) S.jobPolling = setTimeout(poll, 2000);
      return;
    }
    if (S.viewJob !== id) return;  // switched to another job meanwhile
    if (job.lines.length) appendActivity(job.lines);
    if (S.steps) {
      S.steps.action ??= job.action;
      if (job.status === "running") S.steps.live = true;
      const now = Date.now() / 1000;
      for (const line of job.lines) stepLine(S.steps, line, now);
      if (job.status !== "running") finishSteps(S.steps, job.status, now);
      renderSteps(job);
    }
    offset = job.offset;
    const known = (S.state?.jobs || []).find((j) => j.id === id);
    if (known) Object.assign(known, { status: job.status, finished: job.finished, host_times: job.host_times });
    renderJobMeta();
    renderJobCancel();
    $("#activity-dot").className = `dot ${job.status === "running" ? "busy" : job.status === "ok" ? "running" : "error"}`;
    if (job.status === "running") {
      sawRunning = true;
      S.jobPolling = setTimeout(poll, 700);
      return;
    }
    S.jobPolling = null;
    if (!sawRunning) return;  // a finished job opened from history
    jobDone(job);
    announce(`${job.action} ${job.lab || job.topology} ${JOB_ENDED[job.status] || "failed"}`);
    await refreshState();
    await reloadDetail();
    refreshHosts();
  };
  poll();
}

function appendActivity(lines) {
  const pre = $("#activity");
  if (pre.dataset.fresh !== "1") { pre.textContent = ""; pre.dataset.fresh = "1"; }
  const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
  for (const line of lines) {
    const cls = /^✗|ERRO|Error/.test(line) ? "err" : /^[✓↺]/.test(line) ? "ok" : /^(»|\$)/.test(line) ? "info" : null;
    pre.append(cls ? h("span", { class: cls }, line + "\n") : line + "\n");
  }
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}
