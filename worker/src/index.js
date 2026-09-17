const GH_API = "https://api.github.com";

function headers(env) {
  return {
    Authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "ViaVenetoDealsAmazon-Worker",
  };
}

async function telegram(env, method, payload) {
  const response = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok || !data.ok) {
    throw new Error(`Telegram ${method}: ${data.description || response.status}`);
  }
  return data.result;
}

async function sendMessage(env, chatId, text) {
  return telegram(env, "sendMessage", {
    chat_id: chatId,
    text,
    disable_web_page_preview: true,
  });
}

async function editMessage(env, chatId, messageId, text) {
  return telegram(env, "editMessageText", {
    chat_id: chatId,
    message_id: messageId,
    text,
    disable_web_page_preview: true,
  });
}

async function githubFetch(env, path, options = {}) {
  const response = await fetch(`${GH_API}${path}`, {
    ...options,
    headers: { ...headers(env), ...(options.headers || {}) },
  });
  if (!response.ok && response.status !== 204) {
    const body = await response.text();
    throw new Error(`GitHub ${response.status}: ${body.slice(0, 500)}`);
  }
  if (response.status === 204) return null;
  return response.json();
}

async function workflowRuns(env) {
  const repo = env.GITHUB_REPO;
  const workflow = env.REPORT_WORKFLOW || "report.yml";
  const data = await githubFetch(
    env,
    `/repos/${repo}/actions/workflows/${encodeURIComponent(workflow)}/runs?per_page=10`
  );
  return data?.workflow_runs || [];
}

async function activeRun(env) {
  const runs = await workflowRuns(env);
  return runs.find((r) => r.status === "queued" || r.status === "in_progress") || null;
}

async function dispatchReport(env, { chatId = "", statusMessageId = "", cutoff = "", origin = "manual" } = {}) {
  const repo = env.GITHUB_REPO;
  const workflow = env.REPORT_WORKFLOW || "report.yml";
  await githubFetch(env, `/repos/${repo}/actions/workflows/${encodeURIComponent(workflow)}/dispatches`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      ref: env.GITHUB_REF || "main",
      inputs: {
        chat_id: String(chatId || ""),
        status_message_id: String(statusMessageId || ""),
        cutoff: cutoff || new Date().toISOString(),
        origin,
      },
    }),
  });
}

function localTime(date = new Date()) {
  return new Intl.DateTimeFormat("it-IT", {
    timeZone: "Europe/Rome",
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

function localHour(date = new Date()) {
  return Number(
    new Intl.DateTimeFormat("en-GB", {
      timeZone: "Europe/Rome",
      hour: "2-digit",
      hour12: false,
    }).format(date)
  );
}

function elapsed(createdAt) {
  const sec = Math.max(0, Math.floor((Date.now() - new Date(createdAt).getTime()) / 1000));
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return m ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
}

async function handleStatus(env, chatId) {
  try {
    const runs = await workflowRuns(env);
    const active = runs.find((r) => r.status === "queued" || r.status === "in_progress");
    if (active) {
      await sendMessage(
        env,
        chatId,
        `🟡 REPORT IN CORSO\nStato: ${active.status === "queued" ? "in coda" : "in elaborazione"}\nDurata: ${elapsed(active.created_at)}\nAvvio: ${localTime(new Date(active.created_at))}`
      );
      return;
    }

    const last = runs.find((r) => r.status === "completed");
    if (!last) {
      await sendMessage(env, chatId, "ℹ️ Nessun report ancora registrato su GitHub Actions.");
      return;
    }

    const ok = last.conclusion === "success";
    const icon = ok ? "✅" : "❌";
    await sendMessage(
      env,
      chatId,
      `${icon} Ultimo report ${ok ? "completato" : "terminato con errore"}\nConclusione: ${last.conclusion || "sconosciuta"}\nAvvio: ${localTime(new Date(last.created_at))}\nFine: ${last.updated_at ? localTime(new Date(last.updated_at)) : "?"}`
    );
  } catch (error) {
    await sendMessage(env, chatId, `❌ Impossibile leggere lo stato del report.\n${String(error).slice(0, 700)}`);
  }
}

async function handleReportCommand(env, chatId) {
  if (env.OWNER_CHAT_ID && String(chatId) !== String(env.OWNER_CHAT_ID)) {
    await sendMessage(env, chatId, "⛔ Questo bot è privato.");
    return;
  }

  try {
    const running = await activeRun(env);
    if (running) {
      await sendMessage(
        env,
        chatId,
        `⏳ C'è già un report in corso.\nStato: ${running.status === "queued" ? "in coda" : "in elaborazione"}\nDurata: ${elapsed(running.created_at)}\nUsa /status per seguirlo.`
      );
      return;
    }

    const cutoff = new Date();
    const status = await sendMessage(
      env,
      chatId,
      `🟡 Report avviato.\nAnalizzo i messaggi da oggi 00:00 fino alle ${localTime(cutoff).slice(12, 17)}.\nLo stato verrà aggiornato qui; /status resta disponibile.`
    );

    try {
      await dispatchReport(env, {
        chatId,
        statusMessageId: status.message_id,
        cutoff: cutoff.toISOString(),
        origin: "manual",
      });
    } catch (error) {
      await editMessage(
        env,
        chatId,
        status.message_id,
        `❌ Impossibile avviare il report su GitHub Actions.\n${String(error).slice(0, 700)}`
      );
    }
  } catch (error) {
    await sendMessage(env, chatId, `❌ Errore nell'avvio del report.\n${String(error).slice(0, 700)}`);
  }
}

async function handleTelegramUpdate(request, env) {
  if (env.TELEGRAM_WEBHOOK_SECRET) {
    const supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token") || "";
    if (supplied !== env.TELEGRAM_WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }
  }

  const update = await request.json();
  const message = update.message || update.edited_message;
  if (!message?.chat?.id || !message?.text) return new Response("ok");

  const chatId = String(message.chat.id);
  const command = String(message.text).trim().split(/\s+/)[0].split("@")[0].toLowerCase();

  if (env.OWNER_CHAT_ID && chatId !== String(env.OWNER_CHAT_ID)) {
    await sendMessage(env, chatId, "⛔ Questo bot è privato.");
    return new Response("ok");
  }

  if (command === "/sconti" || command === "/report") {
    await handleReportCommand(env, chatId);
  } else if (command === "/status") {
    await handleStatus(env, chatId);
  } else if (command === "/start") {
    await sendMessage(
      env,
      chatId,
      "Via Veneto Deals attivo.\n\n/sconti — avvia il report da mezzanotte fino ad ora\n/report — stesso report\n/status — stato dell'elaborazione\n\nIl report automatico parte alle 20:00 Europe/Rome."
    );
  }
  return new Response("ok");
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/telegram") {
      return handleTelegramUpdate(request, env);
    }
    if (url.pathname === "/health") {
      return Response.json({ ok: true, service: "via-veneto-deals-bot", now: new Date().toISOString() });
    }
    return new Response("Via Veneto Deals bot", { status: 200 });
  },

  async scheduled(event, env, ctx) {
    const when = new Date(event.scheduledTime);
    if (localHour(when) !== 20) return;
    ctx.waitUntil(
      (async () => {
        try {
          if (await activeRun(env)) return;
          await dispatchReport(env, {
            cutoff: when.toISOString(),
            origin: "scheduled",
          });
        } catch (error) {
          console.error("Scheduled report dispatch failed", error);
        }
      })()
    );
  },
};
