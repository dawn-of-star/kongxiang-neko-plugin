// sync-static.mjs —— 把 Public 仓库的游戏静态同步到插件 static/
// 用法：node sync-static.mjs [Public 路径]
import { cpSync, rmSync, mkdirSync, existsSync } from 'node:fs';
import { join, resolve } from 'node:path';

const publicDir = resolve(process.argv[2] || 'C:/Users/25187/Documents/deepseek-harness/default-workspace/Public');
const out = join(import.meta.dirname, 'static');
if (!existsSync(publicDir)) { console.error('Public 不存在:', publicDir); process.exit(1); }
rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });
for (const name of ['index.html', 'css', 'src', 'dist']) {
  cpSync(join(publicDir, name), join(out, name), { recursive: true });
}
console.log('synced static from', publicDir);
