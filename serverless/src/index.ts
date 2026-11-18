import { Hono } from "hono";
import { z } from "zod";
import { handleUpdate } from "./bot";
import type { Env } from "./env";
import { BotError, errorMessage } from "./lib/errors";
import { parseEpisodeRange } from "./lib/range";
import { hmacToken, parseIdList, safeEqual } from "./lib/secure";
import { validateSourceUrl } from "./lib/url";
import { sendMessage } from "./telegram";
import { Runner } from "./runner";
import type { JobEvent, JobKind } from "./runner";

export { Runner };

const app = new Hono<{ Bindings: Env }>();

const jobSchema = z.object({
  url: z.string().min(1),
  episodes: z
    .union([z.string(), z.array(z.number().int().positive())])
    .optional(),
  chat_id: z.number().int().optional(),
});

function episodesToText(
  value: string | number[] | undefined,
): string | null {
  if (value === undefined) return null;
  if (Array.isArray(value)) return value.join(",");
  return value;
}

function allowedChats(env: Env): number[] {
  const explicit = parseIdList(env.ALLOWED_CHAT_IDS);
  return explicit.length ? explicit : parseIdList(env.ALLOWED_USER_IDS);
}

app.get("/healthz", (context) => context.json({ ok: true }));

app.post("/telegram/webhook", async (context) => {
  const secret = context.req.header("x-telegram-bot-api-secret-token") ?? "";
  if (
    !context.env.TELEGRAM_WEBHOOK_SECRET ||
    !safeEqual(secret, context.env.TELEGRAM_WEBHOOK_SECRET)
  ) {
    return context.text("forbidden", 403);
  }
  const update = (await context.req.json().catch(() => null)) as {
    update_id?: number;
  } | null;
  if (!update) return context.json({ ok: true });

  const origin =
    context.env.PUBLIC_BASE_URL?.replace(/\/$/, "") ||
    new URL(context.req.url).origin;
  context.executionCtx.waitUntil(
    handleUpdate(context.env, update, origin).catch((error) => {
      console.error("update failed", error);
    }),
  );
  return context.json({ ok: true });
});

app.use("/v1/*", async (context, next) => {
  const key = context.req.header("x-api-key") ?? "";
  if (!context.env.API_KEY || !safeEqual(key, context.env.API_KEY)) {
    return context.json({ error: "unauthorized" }, 401);
  }
  await next();
});

app.post("/v1/resolve", async (context) => {
  return createJob(context.env, context.req.raw, "resolve");
});

app.post("/v1/jobs", async (context) => {
  return createJob(context.env, context.req.raw, "download");
});

app.get("/v1/jobs/:id", async (context) => {
  const runner = context.env.RUNNER.get(
    context.env.RUNNER.idFromName("global"),
  );
  const view = await runner.jobView(context.req.param("id"));
  if (!view) return context.json({ error: "not found" }, 404);
  return context.json(view);
});

app.delete("/v1/jobs/:id", async (context) => {
  const runner = context.env.RUNNER.get(
    context.env.RUNNER.idFromName("global"),
  );
  const cancelled = await runner.cancelJob(context.req.param("id"));
  if (!cancelled) return context.json({ error: "not found or finished" }, 404);
  return context.json({ jobId: context.req.param("id"), status: "cancelling" });
});

app.post("/internal/jobs/:id/event", async (context) => {
  const jobId = context.req.param("id");
  const token = context.req.header("x-job-token") ?? "";
  const expected = await hmacToken(context.env.INTERNAL_SECRET, jobId);
  if (!safeEqual(token, expected)) {
    return context.json({ error: "forbidden" }, 403);
  }
  const event = (await context.req.json().catch(() => null)) as JobEvent | null;
  if (!event || typeof event.type !== "string") {
    return context.json({ error: "invalid event" }, 400);
  }
  const runner = context.env.RUNNER.get(
    context.env.RUNNER.idFromName("global"),
  );
  await runner.handleEvent(jobId, event);
  return context.json({ ok: true });
});

app.notFound((context) => context.json({ error: "not found" }, 404));

app.onError((error, context) => {
  console.error("request failed", error);
  return context.json({ error: errorMessage(error) }, 500);
});

async function createJob(
  env: Env,
  request: Request,
  kind: JobKind,
): Promise<Response> {
  const payload = (await request.json().catch(() => null)) as unknown;
  const parsed = jobSchema.safeParse(payload);
  if (!parsed.success) {
    return Response.json(
      { error: "invalid body", issues: parsed.error.issues },
      { status: 400 },
    );
  }
  try {
    const url = validateSourceUrl(parsed.data.url, env.ALLOWED_HOSTS);
    const episodes = parseEpisodeRange(
      episodesToText(parsed.data.episodes),
    );
    const chatId =
      parsed.data.chat_id ?? Number(env.DEFAULT_CHAT_ID ?? 0);
    if (!Number.isInteger(chatId) || chatId === 0) {
      throw new BotError(
        "chat_id is required (or set DEFAULT_CHAT_ID). Videos are delivered to Telegram.",
      );
    }
    const allowed = allowedChats(env);
    if (allowed.length && !allowed.includes(chatId)) {
      throw new BotError("chat_id is not allowed");
    }

    const runner = env.RUNNER.get(env.RUNNER.idFromName("global"));
    const jobId = crypto.randomUUID();
    const messageId = await sendMessage(
      env.TELEGRAM_BOT_TOKEN,
      chatId,
      kind === "resolve"
        ? "🔎 API: reading the series page…"
        : "⏳ API: queued download…",
    );
    const origin =
      env.PUBLIC_BASE_URL?.replace(/\/$/, "") ||
      new URL(request.url).origin;
    await runner.enqueue({
      jobId,
      kind,
      url: url.toString(),
      chatId,
      messageId,
      episodes,
      callbackBase: origin,
    });
    return Response.json(
      {
        jobId,
        chatId,
        status: "queued",
        poll: `/v1/jobs/${jobId}`,
      },
      { status: 202 },
    );
  } catch (error) {
    const status = error instanceof BotError ? 400 : 500;
    return Response.json({ error: errorMessage(error) }, { status });
  }
}

export default app;
