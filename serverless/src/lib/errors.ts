/** Errors that are safe to show to a Telegram user or API client. */
export class BotError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "BotError";
  }
}

export function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}
