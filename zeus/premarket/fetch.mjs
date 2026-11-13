#!/usr/bin/env node
/**
 * Benzinga Options School chat scraper (pre-market commentary + options flow report).
 *
 * Uses the dedicated ZEUS browser profile (~/.zeus-terminal/chrome-benzinga) plus the
 * Benzinga cookies saved in Settings.
 *
 * Usage: node fetch.mjs [--max-scroll N]
 *   stdout → JSON {channels: {key: {label, text, attachments, scraped_at}}}
 *   stderr → progress logs
 */
import fs from 'fs';
import path from 'path';
import os from 'os';
import { fileURLToPath } from 'url';
import puppeteer from 'puppeteer-extra';
import StealthPlugin from 'puppeteer-extra-plugin-stealth';
import { execFileSync, execSync } from 'child_process';

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
const COOKIE_PATH = path.join(ZEUS_HOME, 'benzinga_cookies.json');
const LOGIN_SCRIPT = path.join(__dirname, 'login.mjs');
const CHAT_URL = 'https://www.benzinga.com/research/options-school/chat';

const CHANNELS = {
  'chris-market-commentary': { label: 'Chris Market Commentary', text: 'Chris Market Commentary' },
  'options-flow-report': { label: 'Options Flow Report', text: 'Options Flow Report' },
};

const MAX_SCROLL = parseInt(process.argv.find(a => a.startsWith('--max-scroll='))?.split('=')[1] || '8', 10);

function log(msg) { process.stderr.write(`[fetch] ${msg}\n`); }

function isChromeRunning() {
  return false;   // the dedicated profile is never locked by your everyday Chrome
}

function loadCookies() {
  if (!fs.existsSync(COOKIE_PATH)) return [];
  const raw = JSON.parse(fs.readFileSync(COOKIE_PATH, 'utf-8'));
  return raw.filter(c => (c.domain || '').includes('benzinga.com'));
}

function cookiesExpired() {
  const cookies = loadCookies();
  const token = cookies.find(c => c.name === 'benzinga_token');
  if (!token) return true;
  const exp = token.expires || token.expirationDate || 0;
  return exp > 0 && exp < Date.now() / 1000;
}

async function relogin() {
  log('cookies expired or missing, running login.mjs...');
  try {
    execFileSync('node', [LOGIN_SCRIPT, COOKIE_PATH], { stdio: ['ignore', 'inherit', 'inherit'], timeout: 120_000 });
    return true;
  } catch (e) {
    log(`login failed: ${e.message}`);
    return false;
  }
}

async function main() {
  if (cookiesExpired()) {
    if (!await relogin()) { process.exit(2); }
  }

  const chromeRunning = isChromeRunning();
  const useProfile = fs.existsSync(path.join(CHROME_USER_DATA, CHROME_PROFILE));

  const launchArgs = [
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--disable-blink-features=AutomationControlled',
    '--window-size=1440,900',
    '--window-position=-9999,-9999',
  ];

  const launchOpts = {
    headless: 'new',
    executablePath: CHROME_PATH,
    args: launchArgs,
    defaultViewport: { width: 1440, height: 900 },
  };

  if (useProfile) {
    launchOpts.userDataDir = CHROME_USER_DATA;
    launchArgs.push(`--profile-directory=${CHROME_PROFILE}`);
    log('using the dedicated ZEUS browser profile');
  } else if (chromeRunning) {
    log('Chrome is running — profile locked, falling back to cookie injection');
  }

  const browser = await puppeteer.launch(launchOpts);
  const page = await browser.newPage();
  await page.setExtraHTTPHeaders({ 'Accept-Language': 'en-US,en;q=0.9' });

  if (!useProfile) {
    const ALLOWED_FIELDS = new Set(['name','value','domain','path','expires','httpOnly','secure','sameSite','url']);
    const cookies = loadCookies()
      .filter(c => c.name !== 'cf_clearance')
      .map(c => {
        const clean = {};
        for (const [k, v] of Object.entries(c)) {
          if (ALLOWED_FIELDS.has(k)) clean[k] = v;
        }
        return clean;
      });
    if (cookies.length > 0) {
      await page.setCookie(...cookies);
      log(`loaded ${cookies.length} cookies (injection mode, cf_clearance excluded)`);
    }
  }

  log('navigating to chat...');
  await page.goto(CHAT_URL, { waitUntil: 'networkidle2', timeout: 60_000 });
  await new Promise(r => setTimeout(r, 3000));

  // check if redirected to login
  const url = page.url();
  if (/login|signin|sign-in/i.test(url)) {
    log(`redirected to login (${url}), attempting auto-relogin...`);
    await browser.close();
    if (await relogin()) {
      log('relogin succeeded, restarting fetch...');
      execFileSync('node', [fileURLToPath(import.meta.url), ...process.argv.slice(2)], { stdio: 'inherit' });
      return;
    }
    log('relogin failed');
    process.exit(2);
  }

  // wait for sidebar
  log('waiting for sidebar...');
  let sidebarOk = false;
  for (let attempt = 0; attempt < 3; attempt++) {
    for (const ch of Object.values(CHANNELS)) {
      try {
        await page.waitForXPath(`//*[contains(text(), "${ch.text}")]`, { timeout: 15_000 });
        sidebarOk = true;
        break;
      } catch {}
    }
    if (sidebarOk) break;

    const retryBtn = await page.evaluateHandle(() => {
      const els = [...document.querySelectorAll('*')];
      return els.find(e => e.textContent.trim() === 'Retry' && e.offsetParent !== null) || null;
    });
    if (retryBtn.asElement()) {
      log(`sidebar not found (attempt ${attempt + 1}), clicking Retry...`);
      await retryBtn.asElement().click();
      await new Promise(r => setTimeout(r, 5000));
    } else {
      log(`sidebar not found (attempt ${attempt + 1}), no Retry button`);
    }
  }

  if (!sidebarOk) {
    const debugPath = path.join(__dirname, '..', 'data', 'benzinga', '_debug_page.png');
    try {
      await page.screenshot({ path: debugPath, fullPage: true });
      log(`sidebar never appeared — debug screenshot → ${debugPath}`);
    } catch (e) { log(`screenshot failed: ${e.message}`); }
    const title = await page.title();
    const bodyText = await page.evaluate(() => document.body?.innerText?.substring(0, 500) || '');
    log(`page title: ${title}`);
    log(`page body (first 500): ${bodyText.replace(/\n/g, ' | ')}`);
  }

  const result = { fetched_at: new Date().toISOString(), channels: {} };

  for (const [key, meta] of Object.entries(CHANNELS)) {
    log(`clicking channel: ${key}`);
    try {
      const [chEl] = await page.$x(`//*[contains(text(), "${meta.text}")]`);
      if (!chEl) {
        result.channels[key] = { label: meta.label, error: `channel "${meta.text}" not found in sidebar` };
        continue;
      }
      await chEl.click();
      await new Promise(r => setTimeout(r, 3000));

      let panelSel = null;
      for (const sel of ["[class*='str-chat__list']", "[class*='str-chat__main-panel']", "[class*='message-list']", "main"]) {
        const el = await page.$(sel);
        if (el) { panelSel = sel; break; }
      }
      if (!panelSel) panelSel = 'body';

      const jumpBtn = await page.$("[class*='jump-to-latest']");
      if (jumpBtn) {
        try { await jumpBtn.click(); await new Promise(r => setTimeout(r, 1500)); } catch {}
      }
      await page.evaluate(sel => { const el = document.querySelector(sel); if (el) el.scrollTop = el.scrollHeight; }, panelSel);
      await new Promise(r => setTimeout(r, 1800));

      let lastH = -1, stableRounds = 0;
      for (let round = 0; round < MAX_SCROLL; round++) {
        const h = await page.evaluate(sel => { const el = document.querySelector(sel); if (el) { el.scrollTop = 0; return el.scrollHeight; } return 0; }, panelSel);
        await new Promise(r => setTimeout(r, 1500));
        const newH = await page.evaluate(sel => { const el = document.querySelector(sel); return el ? el.scrollHeight : 0; }, panelSel);

        if (newH === lastH) {
          stableRounds++;
          if (stableRounds >= 2) {
            log(`${key}: scroll stable at ${newH}px after ${round + 1} rounds`);
            break;
          }
        } else {
          stableRounds = 0;
        }
        lastH = newH;
        if (round === MAX_SCROLL - 1) {
          log(`${key}: hit scroll cap (${MAX_SCROLL})`);
        }
      }

      const text = await page.evaluate(sel => {
        const el = document.querySelector(sel);
        return el ? el.innerText : '';
      }, panelSel);

      const attachments = await page.evaluate(() =>
        Array.from(document.querySelectorAll('a'))
          .map(a => ({ href: a.href, name: a.textContent.trim() }))
          .filter(a => /\.pdf|\.png|\.jpg/.test(a.href))
      );

      result.channels[key] = {
        label: meta.label,
        text: text.trim(),
        attachments,
        scraped_at: new Date().toISOString(),
      };
      log(`${key}: ${text.length} chars, ${attachments.length} attachments`);

    } catch (e) {
      log(`${key}: ERROR - ${e.message}`);
      result.channels[key] = { label: meta.label, error: e.message };
    }
  }

  // save refreshed cookies back
  try {
    const freshCookies = await page.cookies();
    const hasAuth = freshCookies.some(c => c.name === 'benzinga_token');
    const hasText = Object.values(result.channels).some(c => c.text);
    if (hasText && hasAuth && freshCookies.length >= 10) {
      if (fs.existsSync(COOKIE_PATH)) {
        try { fs.copyFileSync(COOKIE_PATH, COOKIE_PATH + '.bak'); } catch {}
      }
      fs.writeFileSync(COOKIE_PATH, JSON.stringify(freshCookies, null, 2));
      log(`refreshed ${freshCookies.length} cookies → ${COOKIE_PATH}`);
    }
  } catch (e) {
    log(`cookie save-back failed: ${e.message}`);
  }

  await browser.close();
  process.stdout.write(JSON.stringify(result, null, 2));
}

main().catch(err => {
  log(`FATAL: ${err.message}`);
  process.exit(1);
});
