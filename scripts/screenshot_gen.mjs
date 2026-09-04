// Manual settings-page screenshot generator. Not a CI test:
// playwright.config.mjs testMatch only includes frontend_browser.test.mjs.
// Run: npx playwright test scripts/screenshot_gen.mjs
import { test } from "@playwright/test";
import { configPayload } from "../tests/fixtures/config-payload.mjs";
import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import { createServer } from "node:http";
import { extname, resolve } from "node:path";

const ROOT = resolve(import.meta.dirname, "..");
const PAGE_PATH =
	"/pages/%E4%B8%BB%E5%8A%A8%E5%9B%9E%E5%A4%8D%E8%AE%BE%E7%BD%AE/index.html";
const MIME = {
	".css": "text/css; charset=utf-8",
	".html": "text/html; charset=utf-8",
	".js": "text/javascript; charset=utf-8",
	".mjs": "text/javascript; charset=utf-8",
	".png": "image/png",
	".svg": "image/svg+xml",
};

let server;
let baseUrl;

// README screenshot values differ from the browser-test defaults only in
// display content (realistic provider names, a fuller prompt, vision on).
// The field set itself comes from the shared fixture.
function screenshotConfig() {
	return configPayload({
		config_revision: `sha256:${"a".repeat(64)}`,
		whitelist_sessions: ["123456789", "group:987654321"],
		decision_prompt_template:
			"你是一个群聊 Bot，请根据 {latest_message} 和群聊上下文判断是否需要接话。",
		decision_prompt_default:
			"你是一个群聊 Bot，请根据 {latest_message} 和群聊上下文判断是否需要接话。",
		judge_provider_id: "deepseek-chat",
		vision_main_enabled: true,
		vision_skip_stickers: true,
		vision_provider_id: "gpt-4o",
	});
}

test.beforeAll(async () => {
	server = createServer(async (req, res) => {
		const url = new URL(req.url, "http://localhost");
		if (url.pathname === "/api/plugin/page/bridge-sdk.js") {
			res.writeHead(200, { "Content-Type": MIME[".js"] });
			res.end("");
			return;
		}
		if (url.pathname.startsWith("/api/plug/")) {
			const endpoint = url.pathname.split("/").slice(4).join("/");
			if (endpoint === "providers") {
				res.writeHead(200, {
					"Content-Type": "application/json; charset=utf-8",
				});
				res.end(
					JSON.stringify({
						ok: true,
						providers: [
							{ id: "deepseek-chat", label: "DeepSeek V3" },
							{ id: "gpt-4o", label: "GPT-4o" },
						],
					}),
				);
				return;
			}
			if (endpoint === "config") {
				res.writeHead(200, {
					"Content-Type": "application/json; charset=utf-8",
				});
				res.end(JSON.stringify(screenshotConfig()));
				return;
			}
			res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
			res.end(JSON.stringify({ ok: true }));
			return;
		}

		const decoded = decodeURIComponent(
			url.pathname === "/" ? PAGE_PATH : url.pathname,
		);
		const filePath = resolve(ROOT, "." + decoded);
		try {
			const fileStat = await stat(filePath);
			if (!fileStat.isFile()) throw new Error("not a file");
			res.writeHead(200, {
				"Content-Type": MIME[extname(filePath)] || "application/octet-stream",
			});
			createReadStream(filePath).pipe(res);
		} catch {
			res.writeHead(404).end();
		}
	});
	await new Promise((resolveListen) =>
		server.listen(0, "127.0.0.1", resolveListen),
	);
	baseUrl = "http://127.0.0.1:" + server.address().port;
});

test.afterAll(async () => {
	await new Promise((resolveClose) => server.close(resolveClose));
});

for (const { name, width, height, theme } of [
	{
		name: "01-\u684c\u9762\u6d45\u8272\u4e3b\u9898-\u6148\u7231\u4e4b\u60e0",
		width: 1440,
		height: 1000,
		theme: "light",
	},
	{
		name: "02-\u684c\u9762\u6df1\u8272\u4e3b\u9898-\u5ba1\u5224\u4e4b\u53f8",
		width: 1440,
		height: 1000,
		theme: "dark",
	},
	{
		name: "03-\u79fb\u52a8\u7aef\u6d45\u8272",
		width: 375,
		height: 812,
		theme: "light",
	},
	{
		name: "04-\u79fb\u52a8\u7aef\u6df1\u8272",
		width: 375,
		height: 812,
		theme: "dark",
	},
]) {
	test("screenshot " + name, async ({ page }) => {
		await page.setViewportSize({ width, height });
		await page.addInitScript(
			({ theme, config }) => {
				window.AstrBotPluginPage = {
					ready: async () => true,
					apiGet: async (endpoint) => {
						if (endpoint === "providers") {
							return {
								ok: true,
								providers: [
									{ id: "deepseek-chat", label: "DeepSeek V3" },
									{ id: "gpt-4o", label: "GPT-4o" },
								],
							};
						}
						if (endpoint === "config") return config;
						if (endpoint === "ui/theme") return { ok: true, theme };
						return { ok: true };
					},
					apiPost: async () => ({ ok: true }),
				};
			},
			{ theme, config: screenshotConfig() },
		);

		await page.goto(baseUrl + PAGE_PATH);
		await page.waitForSelector("#boot.is-hidden");
		await page.waitForSelector("#enabledPrivateSessionsInput");
		await page.waitForTimeout(300);
		await page.screenshot({
			path: "output/preview/" + name + ".png",
			fullPage: true,
		});
	});
}
