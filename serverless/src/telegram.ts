const TELEGRAM_API = "https://api.telegram.org";

export interface TelegramResponse {
  ok: boolean;
  result?: unknown;
  description?: string;
  error_code?: number;
}

async function call(
  token: string,
  method: string,
  payload: Record<string, unknown>,
): Promise<TelegramResponse> {
  if (!token) return { ok: false, description: "TELEGRAM_BOT_TOKEN is not set" };
  try {
    const response = await fetch(`${TELEGRAM_API}/bot${token}/${method}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(20_000),
    });
    const data = (await response.json().catch(() => null)) as TelegramResponse | null;
    return data ?? { ok: false, description: `HTTP ${response.status}` };
  } catch (error) {
    return {
      ok: false,
      description: error instanceof Error ? error.message : String(error),
    };
  }
}

/** Send a message and return its message id, or null when Telegram refused. */
export async function sendMessage(
  token: string,
  chatId: number,
  text: string,
  options: { disablePreview?: boolean } = {},
): Promise<number | null> {
  const response = await call(token, "sendMessage", {
    chat_id: chatId,
    text,
    parse_mode: "HTML",
    link_preview_options: { is_disabled: options.disablePreview ?? true },
  });
  if (!response.ok) {
    console.warn("sendMessage failed", response.error_code, response.description);
    return null;
  }
  const result = response.result as { message_id?: number } | undefined;
  return typeof result?.message_id === "number" ? result.message_id : null;
}

export async function editMessageText(
  token: string,
  chatId: number,
  messageId: number,
  text: string,
): Promise<TelegramResponse> {
  return call(token, "editMessageText", {
    chat_id: chatId,
    message_id: messageId,
    text,
    parse_mode: "HTML",
    link_preview_options: { is_disabled: true },
  });
}

export async function sendChatAction(
  token: string,
  chatId: number,
  action = "upload_video",
): Promise<void> {
  await call(token, "sendChatAction", { chat_id: chatId, action });
}
