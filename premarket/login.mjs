#!/usr/bin/env node
/**
 * Benzinga Options School — optional automatic re-login.
 * Uses a dedicated browser profile in ~/.zeus-terminal/chrome-benzinga (so cf_clearance
 * persists) and the login you saved in ZEUS Settings.
 * Usage: node login.mjs [output_cookie_path]
 */
import fs from 'fs';
import path from 'path';
import os from 'os';
import { fileURLToPath } from 'url';
import puppeteer from 'puppeteer-extra';
import StealthPlugin from 'puppeteer-extra-plugin-stealth';
import { execSync } from 'child_process';

puppeteer.use(StealthPlugin());

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ZEUS_HOME = process.env.ZEUS_HOME || path.join(os.homedir(), '.zeus-terminal');
const ZEUS_CONFIG = path.join(ZEUS_HOME, 'config.json');
function readConfig() {
  try { return JSON.parse(fs.readFileSync(ZEUS_CONFIG, 'utf-8')); } catch { return {}; }
}
// A dedicated browser profile for Benzinga (never your everyday Chrome profile).
const CHROME_USER_DATA = path.join(ZEUS_HOME, 'chrome-benzinga');
const CHROME_PROFILE = 'Default';
const CHROME_PATH = process.env.CHROME_PATH || (process.platform === 'darwin'
  ? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
  : (process.platform === 'win32' ? 'C:/Program Files/Google/Chrome/Application/chrome.exe' : '/usr/bin/google-chrome'));
const DEFAULT_COOKIE_PATH = path.join(ZEUS_HOME, 'benzinga_cookies.json');
const COOKIE_PATH = process.argv[2] || DEFAULT_COOKIE_PATH;

function saveCookies(cookies) {
  fs.mkdirSync(path.dirname(COOKIE_PATH), { recursive: true });
  fs.writeFileSync(COOKIE_PATH, JSON.stringify(cookies, null, 2), { mode: 0o600 });
  // keep Settings in sync so the server keeps using the fresh session
  const cfg = readConfig();
  cfg.benzinga_cookies_json = JSON.stringify(cookies);
  fs.writeFileSync(ZEUS_CONFIG, JSON.stringify(cfg, null, 2), { mode: 0o600 });
}

const LOGIN_URL = 'https://www.benzinga.com/research/options-school/chat';

function isChromeRunning() {
  return false;   // the dedicated profile is never locked by your everyday Chrome
}

function readCredentials() {
  const cfg = readConfig();
  const name = (process.env.ZEUS_BENZINGA_USERNAME || cfg.benzinga_username || '').trim();
  const password = (process.env.ZEUS_BENZINGA_PASSWORD || cfg.benzinga_password || '').trim();
  if (!name || !password) throw new Error('Benzinga login not set: open ZEUS Settings → News sources');
  return { name, password };
}

async function main() {
  const { name, password } = readCredentials();
  const chromeRunning = isChromeRunning();
  fs.mkdirSync(CHROME_USER_DATA, { recursive: true });
  const useProfile = true;

  const launchArgs = [
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--disable-blink-features=AutomationControlled',
    '--window-size=1280,800',
    '--window-position=-9999,-9999',
  ];

  const launchOpts = {
    headless: 'new',
    executablePath: CHROME_PATH,
    args: launchArgs,
    defaultViewport: { width: 1280, height: 800 },
  };

  if (useProfile) {
    launchOpts.userDataDir = CHROME_USER_DATA;
    launchArgs.push(`--profile-directory=${CHROME_PROFILE}`);
    console.log('[login] using the dedicated ZEUS browser profile');
  } else if (chromeRunning) {
    console.log('[login] Chrome is running — using standalone instance with stealth');
  }

  const browser = await puppeteer.launch(launchOpts);
  const page = await browser.newPage();
  await page.setExtraHTTPHeaders({ 'Accept-Language': 'en-US,en;q=0.9' });

  console.log('[login] navigating to login page...');
  await page.goto(LOGIN_URL, { waitUntil: 'networkidle2', timeout: 60000 });

  // If using real profile, might already be logged in
  if (useProfile && !/login|signin|sign-in/i.test(page.url())) {
    console.log('[login] already logged in via profile, refreshing cookies...');
    const cookies = await page.cookies();
    const hasAuth = cookies.some(c => c.name === 'benzinga_token');
    if (hasAuth) {
      saveCookies(cookies);
      console.log(`[login] saved ${cookies.length} cookies`);
      await browser.close();
      console.log('[login] done (profile session valid).');
      return;
    }
  }

  // login form is inside an iframe
  let targetFrame = page;
  for (const frame of page.frames()) {
    const inputs = await frame.$$('input');
    if (inputs.length > 0) {
      targetFrame = frame;
      break;
    }
  }

  console.log('[login] filling credentials...');
  await targetFrame.waitForSelector('input[type="email"], input[placeholder*="email" i], input[name="email"], input[name="username"], input[type="text"]', { timeout: 30000 });

  let emailInput = await targetFrame.$('input[type="email"]')
    || await targetFrame.$('input[placeholder*="email" i]')
    || await targetFrame.$('input[name="email"]')
    || await targetFrame.$('input[name="username"]')
    || await targetFrame.$('input[type="text"]');

  await emailInput.click({ clickCount: 3 });
  await emailInput.type(name, { delay: 80 });

  const pwInput = await targetFrame.$('input[type="password"]');
  if (pwInput) {
    await pwInput.click({ clickCount: 3 });
    await pwInput.type(password, { delay: 80 });
  }

  console.log('[login] submitting...');
  const loginBtn = await targetFrame.evaluateHandle(() => {
    const buttons = [...document.querySelectorAll('button')];
    return buttons.find(b => /log\s*in|login|sign\s*in/i.test(b.textContent)) || null;
  });
  if (loginBtn && loginBtn.asElement()) {
    await loginBtn.asElement().click();
  } else if (pwInput) {
    await pwInput.press('Enter');
  }

  await page.waitForNavigation({ waitUntil: 'networkidle2', timeout: 60000 }).catch(() => {});
  await new Promise(r => setTimeout(r, 5000));

  const cookies = await page.cookies();
  const hasAuth = cookies.some(c => c.name === 'benzinga_token');
  if (!hasAuth || cookies.length < 10) {
    console.error(`[login] FAIL: cookies incomplete (auth=${hasAuth}, n=${cookies.length})`);
    await browser.close();
    process.exit(2);
  }
  saveCookies(cookies);
  console.log(`[login] saved ${cookies.length} cookies`);

  await browser.close();
  console.log('[login] done.');
}

main().catch(err => {
  console.error('[login] FATAL:', err.message);
  process.exit(1);
});
