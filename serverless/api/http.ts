import app from "../src/app";
import { VercelRunner } from "../src/vercel/runner";

export default {
  fetch(request: Request) {
    const botToken = process.env.TELEGRAM_BOT_TOKEN ?? "";
    const url = new URL(request.url);
    const route = url.searchParams.get("__route");
    if (url.pathname === "/api/http" && route?.startsWith("/")) {
      url.pathname = route;
      url.searchParams.delete("__route");
      request = new Request(url, request);
    }
    return app.fetch(request, {
      runner: new VercelRunner(botToken),
      TELEGRAM_BOT_TOKEN: botToken,
      TELEGRAM_WEBHOOK_SECRET: process.env.TELEGRAM_WEBHOOK_SECRET ?? "",
      INTERNAL_SECRET: process.env.INTERNAL_SECRET ?? "",
      API_KEY: process.env.API_KEY ?? "",
      ALLOWED_HOSTS: process.env.ALLOWED_HOSTS ?? "huangguoai.com",
      ALLOWED_USER_IDS: process.env.ALLOWED_USER_IDS ?? "",
      ALLOWED_CHAT_IDS: process.env.ALLOWED_CHAT_IDS,
      DEFAULT_CHAT_ID: process.env.DEFAULT_CHAT_ID,
      PUBLIC_BASE_URL: process.env.PUBLIC_BASE_URL,
    });
  },
};
