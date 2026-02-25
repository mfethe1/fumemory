#!/usr/bin/env node
import fs from 'fs';
import path from 'path';

const event = process.argv[2] || 'event';
const level = process.argv[3] || 'info';
const payload = process.argv[4] ? JSON.parse(process.argv[4]) : {};
const ts = new Date().toISOString();
const entry = { ts, event, level, payload };
const root = process.env.MEMU_LOG_DIR || 'data/logs';
fs.mkdirSync(root, { recursive: true });
for (const file of [`${event}.jsonl`, 'all.jsonl']) {
  fs.appendFileSync(path.join(root, file), JSON.stringify(entry) + '\n');
}
console.log(JSON.stringify({ ok: true }));
