import {
  chromium,
  type Browser,
  type BrowserContext,
  type Page,
} from "playwright";

const USER_AGENT =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36";

const MEDIA_URL = /\.(m3u8|mp4|mpd|flv)(\?.*)?$/i;
const MEDIA_MIME = ["mpegurl", "video/mp4", "dash+xml", "x-mpegurl"];

export interface EpisodeRef {
  number: number;
  label: string;
  href: string;
  selector?: string;
}

export interface Session {
  readonly browser: Browser;
  readonly context: BrowserContext;
  readonly page: Page;
  readonly captured: Set<string>;
  readonly allowedHosts: string;
  close(): Promise<void>;
}

export interface InitialData {
  url?: string;
  hevc_url?: string;
  duration?: number;
}

function isAllowedHost(hostname: string, allowedHosts: string): boolean {
  const host = hostname.toLowerCase();
  return allowedHosts
    .split(",")
    .map((entry) => entry.trim().toLowerCase().replace(/^\.+/, ""))
    .filter(Boolean)
    .some((entry) => host === entry || host.endsWith(`.${entry}`));
}

function absolute(href: string, base: string): string {
  try {
    return new URL(href, base).toString();
  } catch {
    return "";
  }
}

export async function openSession(
  startUrl: string,
  allowedHosts: string,
): Promise<Session> {
  const browser = await chromium.launch({
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
  });
  const context = await browser.newContext({
    userAgent: USER_AGENT,
    viewport: { width: 1366, height: 900 },
    ignoreHTTPSErrors: false,
  });
  const page = await context.newPage();
  const captured = new Set<string>();

  const remember = (url: string) => {
    if (MEDIA_URL.test(url) && !url.endsWith(".ts")) captured.add(url);
  };
  page.on("request", (request) => remember(request.url()));
  page.on("response", (response) => {
    const contentType = (response.headers()["content-type"] ?? "").toLowerCase();
    if (
      MEDIA_MIME.some((marker) => contentType.includes(marker)) &&
      !response.url().endsWith(".ts")
    ) {
      captured.add(response.url());
    }
  });

  const response = await page.goto(startUrl, {
    waitUntil: "domcontentloaded",
    timeout: 35_000,
  });
  if (response && response.status() >= 400) {
    await context.close().catch(() => undefined);
    await browser.close().catch(() => undefined);
    throw new Error(`The site returned HTTP ${response.status()}`);
  }
  if (!isAllowedHost(new URL(page.url()).hostname, allowedHosts)) {
    await context.close().catch(() => undefined);
    await browser.close().catch(() => undefined);
    throw new Error("The page redirected to a host that is not allowed");
  }

  return {
    browser,
    context,
    page,
    captured,
    allowedHosts,
    close: async () => {
      await context.close().catch(() => undefined);
      await browser.close().catch(() => undefined);
    },
  };
}

export async function discoverEpisodes(
  session: Session,
): Promise<{ title: string; episodes: EpisodeRef[] }> {
  const { page } = session;
  await page.waitForTimeout(2500);

  if ((await page.locator("[data-web-play-ep-grid]").count()) === 0) {
    await page.mouse.wheel(0, 400).catch(() => undefined);
    await page.waitForTimeout(600);
  }

  for (const label of ["选集", "選集", "剧集", "劇集", "全部剧集", "Episodes"]) {
    try {
      const control = page.getByText(label, { exact: true }).first();
      if (await control.isVisible({ timeout: 400 }).catch(() => false)) {
        await control.click({ timeout: 1500 });
        await page.waitForTimeout(800);
        break;
      }
    } catch {
      // The control is optional.
    }
  }

  let episodes: EpisodeRef[] = [];

  const grid = await page.evaluate(() => {
    const anchors = Array.from(
      document.querySelectorAll<HTMLAnchorElement>(
        "[data-web-play-ep-grid] a[data-ep-id]:not(.is-locked)[href]",
      ),
    );
    return anchors
      .map((anchor) => ({
        number: Number(anchor.dataset.epId),
        label: (anchor.textContent ?? "").trim() || `Episode ${anchor.dataset.epId}`,
        href: anchor.getAttribute("href") ?? "",
      }))
      .filter((entry) => Number.isInteger(entry.number) && entry.number > 0)
      .sort((left, right) => left.number - right.number);
  });
  if (grid.length) {
    episodes = grid.map((entry) => ({
      ...entry,
      href: absolute(entry.href, page.url()),
    }));
  }

  if (!episodes.length) {
    const detected = await page.evaluate(() => {
      const results: Array<{
        number: number;
        label: string;
        href: string;
        selector: string;
      }> = [];
      const seen = new Set<number>();
      const elements = Array.from(
        document.querySelectorAll<HTMLElement>("a, button, li, div, span"),
      );
      for (const element of elements) {
        const text = (element.innerText || element.textContent || "")
          .trim()
          .replace(/\s+/g, " ");
        if (!text || text.length > 25) continue;
        const match =
          text.match(/第\s*(\d+)\s*集/) || text.match(/^[Ee][Pp]\.?\s*(\d+)/);
        if (!match) continue;
        const childHasMatch = Array.from(element.children).some((child) =>
          /第\s*\d+\s*集/.test((child as HTMLElement).innerText || ""),
        );
        if (childHasMatch) continue;
        const number = Number.parseInt(match[1], 10);
        if (!Number.isInteger(number) || seen.has(number)) continue;
        seen.add(number);
        const clickable =
          element.closest<HTMLElement>("a, button, li") ?? element;
        const selector = `hg-ep-${number}`;
        clickable.setAttribute("data-hg-target", selector);
        results.push({
          number,
          label: `第${number}集`,
          href: clickable.getAttribute("href") ?? "",
          selector: `[data-hg-target="${selector}"]`,
        });
      }
      return results.sort((left, right) => left.number - right.number);
    });
    episodes = detected.map((entry) => ({
      number: entry.number,
      label: entry.label,
      href: absolute(entry.href, page.url()),
      selector: entry.selector,
    }));
  }

  if (!episodes.length) {
    episodes = await descriptionEpisodes(session);
  }

  const rawTitle = (await page.title())
    .split("-")[0]
    .split("|")[0]
    .trim();

  if (!episodes.length) {
    episodes = [{ number: 1, label: "Episode 1", href: page.url() }];
  }

  return { title: rawTitle || "Series", episodes };
}

/**
 * huangguoai series pages advertise "全集 56" in the meta description and use
 * /video/<id>/ep-N paths, which works even when the grid is rendered late.
 */
async function descriptionEpisodes(session: Session): Promise<EpisodeRef[]> {
  const { page } = session;
  const url = new URL(page.url());
  if (!isAllowedHost(url.hostname, session.allowedHosts)) return [];

  const match = /^\/video\/(\d+)(?:\/ep-\d+)?\/?$/.exec(url.pathname);
  if (!match) return [];

  const description = await page
    .locator('meta[name="description"]')
    .getAttribute("content")
    .catch(() => null);
  const count = /全\s*(\d+)\s*集/.exec(description ?? "");
  if (!count) return [];
  const total = Number(count[1]);
  if (!Number.isInteger(total) || total < 1 || total > 1000) return [];

  const base = `${url.origin}/video/${match[1]}/`;
  return Array.from({ length: total }, (_, index) => {
    const number = index + 1;
    return {
      number,
      label: `第${number}集`,
      href: number === 1 ? base : `${base}ep-${number}`,
    };
  });
}

export async function readInitialData(page: Page): Promise<InitialData | null> {
  try {
    const raw = await page.evaluate(() => {
      const element = document.getElementById("videoInitialData");
      return element?.textContent ?? null;
    });
    if (!raw) return null;
    const data = JSON.parse(raw) as {
      url?: string;
      hevc_url?: string;
      drama_duration?: number;
    };
    return {
      url: typeof data.url === "string" ? data.url : undefined,
      hevc_url: typeof data.hevc_url === "string" ? data.hevc_url : undefined,
      duration: Number(data.drama_duration ?? 0) || undefined,
    };
  } catch {
    return null;
  }
}

async function clickPlay(page: Page): Promise<void> {
  const selectors = [
    "video",
    ".vjs-big-play-button",
    ".dplayer-video-wrap",
    ".art-video-player",
    ".plyr__control--overlaid",
    "[aria-label='Play']",
  ];
  for (const selector of selectors) {
    try {
      const locator = page.locator(selector).first();
      if (await locator.isVisible({ timeout: 500 }).catch(() => false)) {
        await locator.click({ timeout: 2000, force: true });
        await page.waitForTimeout(2500);
        return;
      }
    } catch {
      // Try the next control.
    }
  }
}

async function openEpisode(session: Session, episode: EpisodeRef): Promise<void> {
  const { page } = session;
  if (episode.href) {
    const response = await page.goto(episode.href, {
      waitUntil: "domcontentloaded",
      timeout: 35_000,
    });
    if (response && response.status() >= 400) {
      throw new Error(`Episode page returned HTTP ${response.status()}`);
    }
  } else if (episode.selector) {
    await page.locator(episode.selector).first().click({ timeout: 5000 });
    await page.waitForTimeout(2500);
  }
  if (!isAllowedHost(new URL(page.url()).hostname, session.allowedHosts)) {
    throw new Error("The episode page redirected to a host that is not allowed");
  }
}

function preferManifest(urls: string[]): string[] {
  return [...urls].sort((left, right) => {
    const leftScore = left.toLowerCase().includes(".m3u8") ? 0 : 1;
    const rightScore = right.toLowerCase().includes(".m3u8") ? 0 : 1;
    return leftScore - rightScore;
  });
}

export async function findEpisodeStream(
  session: Session,
  episode: EpisodeRef,
  seen: Set<string>,
): Promise<{ url: string; referer: string }> {
  const { page, captured } = session;
  await openEpisode(session, episode);
  await page.waitForTimeout(2500);

  const pick = (data: InitialData | null): string | null => {
    if (!data) return null;
    if (data.url && !seen.has(data.url)) return data.url;
    if (data.hevc_url && !seen.has(data.hevc_url)) return data.hevc_url;
    return null;
  };

  let stream = pick(await readInitialData(page));

  if (!stream) {
    await clickPlay(page);
    await page.waitForTimeout(3000);
    stream = pick(await readInitialData(page));
  }

  if (!stream) {
    try {
      const domSources = await page.evaluate(() =>
        Array.from(document.querySelectorAll("video, source"))
          .map(
            (element) =>
              (element as HTMLVideoElement).src ||
              (element as HTMLSourceElement).src ||
              "",
          )
          .filter((source) => /^https?:/i.test(source)),
      );
      stream = preferManifest(domSources).find((source) => !seen.has(source)) ?? null;
    } catch {
      stream = null;
    }
  }

  if (!stream) {
    stream =
      preferManifest([...captured]).find((candidate) => !seen.has(candidate)) ??
      null;
  }

  if (!stream) {
    throw new Error(
      "No video stream was found (the episode may need a login or VIP access)",
    );
  }

  seen.add(stream);
  return { url: stream, referer: page.url() };
}
