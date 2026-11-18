import type { Env } from "./env";
import { BotError, errorMessage } from "./lib/errors";
import { formatRanges, parseEpisodeRange } from "./lib/range";
import { escapeHtml, parseIdList } from "./lib/secure";
import { validateSourceUrl } from "./lib/url";
import { sendMessage } from "./telegram";
import type { JobKind, JobRunner } from "./jobs";

const HELP = [
  "<b>Series Downloader bot</b>",
  "",
  "1. Send a series URL with <code>/episodes</code> to list episodes:",
  "   <code>/episodes https://example.com/video/123</code>",
  "2. Download episodes (max 10 per request):",
  "   <code>/dl https://example.com/video/123 1-3</code>",
  "   <code>/dl https://example.com/video/123 1,4,7</code>",
  "   <code>/dl https://example.com/video/123</code> → first 10 episodes",
  "",
  "Videos are compressed to fit Telegram's 50 MB limit.",
  "",
  "Commands: /status · /cancel · /help",
].join("\n");

export async function handleUpdate(
  env: Env,
  update: unknown,
  origin: string,
): Promise<void> {
  const runner = env.runner;
  const body = update as {
    update_id?: number;
    message?: {
      text?: string;
      chat?: { id?: number };
      from?: { id?: number };
    };
  };

  if (await runner.seenUpdate(Number(body.update_id))) return;

  const message = body.message;
  const chatId = Number(message?.chat?.id ?? 0);
  const userId = Number(message?.from?.id ?? 0);
  const rawText = message?.text?.trim();
  if (!chatId || !rawText) return;

  const allowedUsers = parseIdList(env.ALLOWED_USER_IDS);
  if (!allowedUsers.length) {
    await sendMessage(
      env.TELEGRAM_BOT_TOKEN,
      chatId,
      "⚠️ This bot is not configured: set ALLOWED_USER_IDS.",
    );
    return;
  }
  if (!allowedUsers.includes(userId)) {
    await sendMessage(env.TELEGRAM_BOT_TOKEN, chatId, "⛔ This bot is private.");
    return;
  }

  const [head, ...rest] = rawText.split(/\s+/);
  const command = (head ?? "").split("@")[0].toLowerCase();
  const args = rest;

  if (command === "/start" || command === "/help" || command === "help") {
    await sendMessage(env.TELEGRAM_BOT_TOKEN, chatId, HELP);
    return;
  }

  if (command === "/status") {
    const text = await runner.statusText(chatId);
    await sendMessage(env.TELEGRAM_BOT_TOKEN, chatId, text);
    return;
  }

  if (command === "/cancel" || command === "/stop") {
    const text = await runner.cancelChat(chatId);
    await sendMessage(env.TELEGRAM_BOT_TOKEN, chatId, text);
    return;
  }

  if (command === "/episodes" || command === "/resolve") {
    await startJob(env, runner, chatId, "resolve", args[0], null, origin);
    return;
  }

  if (command === "/dl" || command === "/download") {
    await startJob(env, runner, chatId, "download", args[0], args.slice(1).join(" "), origin);
    return;
  }

  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(rawText) || /^[\w-]+(\.[\w-]+)+\//.test(rawText)) {
    await startJob(env, runner, chatId, "download", rawText, null, origin);
    return;
  }

  await sendMessage(env.TELEGRAM_BOT_TOKEN, chatId, HELP);
}

async function startJob(
  env: Env,
  runner: JobRunner,
  chatId: number,
  kind: JobKind,
  rawUrl: string | undefined,
  rangeText: string | null,
  origin: string,
): Promise<void> {
  try {
    if (!rawUrl) {
      throw new BotError(
        `Send a series URL, for example <code>/dl https://${env.ALLOWED_HOSTS.split(",")[0]}/video/123 1-3</code>`,
      );
    }
    const url = validateSourceUrl(rawUrl, env.ALLOWED_HOSTS);
    const episodes = parseEpisodeRange(rangeText);
    const jobId = crypto.randomUUID();
    const intro =
      kind === "resolve"
        ? "🔎 Reading the series page…"
        : episodes
          ? `⏳ Queued episodes ${formatRanges(episodes)}…`
          : "⏳ Queued (first 10 episodes)…";
    const messageId = await sendMessage(env.TELEGRAM_BOT_TOKEN, chatId, intro);
    await runner.enqueue({
      jobId,
      kind,
      url: url.toString(),
      chatId,
      messageId,
      episodes,
      callbackBase: origin,
    });
  } catch (error) {
    await sendMessage(
      env.TELEGRAM_BOT_TOKEN,
      chatId,
      `⚠️ ${escapeHtml(errorMessage(error))}`,
    );
  }
}
