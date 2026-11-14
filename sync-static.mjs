#!/usr/bin/env node
// sync-static.mjs —— 把《空想增量》的静态资源同步进插件 static/
//
// 用法：
//   node sync-static.mjs --source <游戏目录>
//   KONGXIANG_GAME_DIR=<游戏目录> node sync-static.mjs
//   node sync-static.mjs --source <游戏目录> --dest <目标目录>
//
// 约定：
//   · 只复制 STATIC_ENTRIES 声明的静态入口，不改动游戏仓库；
//   · 目标目录默认是本脚本同级的 static/（用 import.meta.url 解析，跨平台）；
//   · 没有 --source / KONGXIANG_GAME_DIR 时直接报错退出，不使用任何本机路径；
//   · 同步只清空目标目录内部的条目，不删除目标目录之外的任何文件。

import {
  copyFileSync,
  existsSync,
  mkdirSync,
  readdirSync,
  rmSync,
  statSync,
} from 'node:fs';
import { dirname, isAbsolute, join, parse, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const DEFAULT_DEST = join(SCRIPT_DIR, 'static');

// 游戏运行时只依赖这些顶层入口。沿用旧版脚本的同步范围，避免把游戏仓库的
// docs/、tools/、electron/ 等开发内容带进插件包。
const STATIC_ENTRIES = ['index.html', 'css', 'src', 'dist'];

// 非静态内容：命中即跳过。过滤作用到每一层，顶层入口里混入的测试/缓存目录
// 也不会被复制。
const EXCLUDED_DIRS = new Set([
  '.git',
  '.github',
  '.vscode',
  '.idea',
  '.husky',
  'node_modules',
  '_dsh-tools',
  'tests',
  'test',
  '__tests__',
  '__pycache__',
  '.pytest_cache',
  '.ruff_cache',
  '.venv',
  'venv',
  'coverage',
  'build',
  'release',
  'docs',
  'electron',
  'legacy',
  'tools',
  '_site',
]);

const EXCLUDED_FILES = new Set([
  '.DS_Store',
  'Thumbs.db',
  'desktop.ini',
  '.gitignore',
  '.gitattributes',
  '.npmrc',
  'package.json',
  'package-lock.json',
  'pnpm-lock.yaml',
  'yarn.lock',
]);

// 开发期文件/产物：不是游戏运行时需要的静态资源。
const EXCLUDED_FILE_PATTERN = /(?:\.(?:test|spec)\.[cm]?jsx?|\.map|\.log|\.tmp|\.bak|\.orig)$/i;

function fail(message) {
  console.error(`✗ ${message}`);
  process.exit(1);
}

function printUsage() {
  console.log(`用法：node sync-static.mjs --source <游戏目录> [--dest <目标目录>]

选项：
  -s, --source <目录>  游戏仓库目录（默认读环境变量 KONGXIANG_GAME_DIR）
  -d, --dest <目录>    目标 static 目录（默认：本脚本同级的 static/）
      --force          目标目录非空且缺少 index.html 时也强制覆盖
  -h, --help           显示本帮助

示例（Windows 反斜杠路径请加引号）：
  node sync-static.mjs --source "C:\\path\\to\\Public"
  KONGXIANG_GAME_DIR=/path/to/Public node sync-static.mjs
  node sync-static.mjs --source ../Public --dest ./static`);
}

function parseArgs(argv) {
  const options = { source: '', dest: '', force: false, help: false };
  const positionals = [];

  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '--help' || arg === '-h') {
      options.help = true;
    } else if (arg === '--force') {
      options.force = true;
    } else if (arg === '--source' || arg === '-s') {
      const value = argv[index + 1];
      if (!value || value.startsWith('--')) fail('--source 需要一个目录参数');
      options.source = value;
      index += 1;
    } else if (arg.startsWith('--source=')) {
      const value = arg.slice('--source='.length);
      if (!value) fail('--source 需要一个目录参数');
      options.source = value;
    } else if (arg === '--dest' || arg === '-d') {
      const value = argv[index + 1];
      if (!value || value.startsWith('--')) fail('--dest 需要一个目录参数');
      options.dest = value;
      index += 1;
    } else if (arg.startsWith('--dest=')) {
      const value = arg.slice('--dest='.length);
      if (!value) fail('--dest 需要一个目录参数');
      options.dest = value;
    } else if (arg.startsWith('-')) {
      fail(`未知参数：${arg}（用 --help 查看用法）`);
    } else {
      positionals.push(arg);
    }
  }

  // 兼容旧用法：node sync-static.mjs <游戏目录>。显式 --source 优先。
  if (positionals.length > 1) fail(`多余的参数：${positionals.slice(1).join(' ')}`);
  if (positionals.length === 1 && !options.source) options.source = positionals[0];
  if (positionals.length === 1 && options.source) fail('源目录被重复指定（位置参数与 --source）');

  return options;
}

function resolveSource(cliSource) {
  const raw = String(cliSource || process.env.KONGXIANG_GAME_DIR || '').trim();
  if (!raw) {
    console.error('✗ 没有指定游戏目录，已中止。');
    console.error('  用 --source <游戏目录> 指定，或设置环境变量 KONGXIANG_GAME_DIR。');
    console.error('  例：node sync-static.mjs --source "C:\\path\\to\\Public"');
    process.exit(1);
  }

  const source = resolve(raw);
  if (!existsSync(source)) fail(`游戏目录不存在：${source}`);
  if (!statSync(source).isDirectory()) fail(`游戏目录不是文件夹：${source}`);
  if (!existsSync(join(source, 'index.html'))) {
    fail(`看起来不是《空想增量》游戏目录（缺少 index.html）：${source}`);
  }
  return source;
}

function resolveDest(cliDest, source) {
  const dest = resolve(cliDest || DEFAULT_DEST);

  if (dest === parse(dest).root) fail(`拒绝把目标设为磁盘根目录：${dest}`);
  if (dest === source) fail('源目录与目标目录相同，已中止。');

  // 目标在源内部 → sync 会把 static 写进游戏仓库；源在目标内部 → 清空目标
  // 会连游戏源码一起删掉。两种情况都必须拒绝。
  const sourceToDest = relative(source, dest);
  if (sourceToDest && !sourceToDest.startsWith('..') && !isAbsolute(sourceToDest)) {
    fail(`目标目录位于源目录内部，已中止：${dest}`);
  }
  const destToSource = relative(dest, source);
  if (destToSource && !destToSource.startsWith('..') && !isAbsolute(destToSource)) {
    fail(`源目录位于目标目录内部（清空会误删源码），已中止：${dest}`);
  }

  if (existsSync(dest) && !statSync(dest).isDirectory()) {
    fail(`目标路径已存在且不是文件夹：${dest}`);
  }
  return dest;
}

function assertReplaceableDest(dest, force) {
  if (!existsSync(dest) || force) return;
  const entries = readdirSync(dest);
  if (entries.length === 0 || entries.includes('index.html')) return;
  fail(
    `目标目录非空且没有 index.html，看起来不是插件的 static/：${dest}\n` +
      '  确认要覆盖请加 --force。',
  );
}

function clearDir(dir) {
  if (!existsSync(dir)) {
    mkdirSync(dir, { recursive: true });
    return;
  }
  for (const name of readdirSync(dir)) {
    rmSync(join(dir, name), { recursive: true, force: true });
  }
}

function shouldSkip(name, isDirectory) {
  // 隐藏文件/目录（.git、.vscode、.DS_Store、缓存等）一律不进入静态页。
  if (name.startsWith('.')) return true;
  if (isDirectory) return EXCLUDED_DIRS.has(name) || name.endsWith('.egg-info');
  return EXCLUDED_FILES.has(name) || EXCLUDED_FILE_PATTERN.test(name);
}

function copyTree(sourcePath, destPath, stats) {
  for (const entry of readdirSync(sourcePath, { withFileTypes: true })) {
    if (shouldSkip(entry.name, entry.isDirectory())) continue;

    const from = join(sourcePath, entry.name);
    const to = join(destPath, entry.name);
    if (entry.isDirectory()) {
      mkdirSync(to, { recursive: true });
      copyTree(from, to, stats);
    } else if (entry.isFile()) {
      copyFileSync(from, to);
      stats.files += 1;
      stats.bytes += statSync(from).size;
    } else {
      // 不跟随符号链接/特殊文件，避免把游戏仓库之外的内容带进插件包。
      stats.skipped += 1;
    }
  }
}

function copyEntry(name, source, dest, stats) {
  const from = join(source, name);
  if (!existsSync(from)) return false;

  const info = statSync(from);
  if (info.isDirectory()) {
    const to = join(dest, name);
    mkdirSync(to, { recursive: true });
    copyTree(from, to, stats);
  } else if (info.isFile()) {
    copyFileSync(from, join(dest, name));
    stats.files += 1;
    stats.bytes += info.size;
  } else {
    stats.skipped += 1;
    return false;
  }
  return true;
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  let value = bytes;
  let unit = 'B';
  for (const next of ['KB', 'MB', 'GB']) {
    value /= 1024;
    unit = next;
    if (value < 1024) break;
  }
  return `${value.toFixed(1)} ${unit}`;
}

function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.help) {
    printUsage();
    return;
  }

  const source = resolveSource(options.source);
  const dest = resolveDest(options.dest, source);

  assertReplaceableDest(dest, options.force);
  console.log('《空想增量》静态资源同步');
  console.log(`  源目录：${source}`);
  console.log(`  目标：  ${dest}`);

  clearDir(dest);

  const stats = { files: 0, bytes: 0, skipped: 0 };
  const lines = [];
  for (const name of STATIC_ENTRIES) {
    const before = stats.files;
    if (copyEntry(name, source, dest, stats)) {
      lines.push(`  ✓ ${name.padEnd(11)} ${stats.files - before} 个文件`);
    } else {
      lines.push(`  - ${name.padEnd(11)} 源中不存在，跳过`);
    }
  }

  for (const line of lines) console.log(line);
  console.log(`  合计：${stats.files} 个文件，${formatBytes(stats.bytes)}`);
  if (stats.skipped > 0) console.log(`  跳过：${stats.skipped} 个符号链接/特殊文件`);
  console.log('完成。');
}

main();
