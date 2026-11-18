import { BotError } from "./errors";

/**
 * Accept only the configured source hosts. Rejects credentials, unusual ports
 * and non-HTTP schemes so the job container cannot be pointed at internal hosts.
 */
export function validateSourceUrl(raw: string, allowedHosts: string): URL {
  const hosts = allowedHosts
    .split(",")
    .map((host) => host.trim().toLowerCase().replace(/^\.+/, ""))
    .filter(Boolean);
  if (!hosts.length) throw new BotError("No source hosts are configured.");

  let candidate = raw.trim();
  if (!candidate) throw new BotError("Send a series URL.");
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(candidate)) candidate = `https://${candidate}`;

  let url: URL;
  try {
    url = new URL(candidate);
  } catch {
    throw new BotError("That does not look like a valid URL.");
  }

  if (url.protocol !== "https:" && url.protocol !== "http:") {
    throw new BotError("Only http(s) URLs are supported.");
  }
  if (url.username || url.password) {
    throw new BotError("URLs with a username or password are not allowed.");
  }
  if (url.port && url.port !== "80" && url.port !== "443") {
    throw new BotError("Only the default HTTP/HTTPS ports are allowed.");
  }

  const hostname = url.hostname.toLowerCase();
  const allowed = hosts.some(
    (host) => hostname === host || hostname.endsWith(`.${host}`),
  );
  if (!allowed) {
    throw new BotError(`Only these hosts are allowed: ${hosts.join(", ")}`);
  }
  return url;
}

export function assertAllowedHost(url: URL | string, allowedHosts: string): void {
  const parsed = typeof url === "string" ? new URL(url) : url;
  const hostname = parsed.hostname.toLowerCase();
  const allowed = allowedHosts
    .split(",")
    .map((host) => host.trim().toLowerCase().replace(/^\.+/, ""))
    .filter(Boolean)
    .some((host) => hostname === host || hostname.endsWith(`.${host}`));
  if (!allowed) throw new BotError(`Unexpected redirect to ${hostname}.`);
}
