// G11 前端语法门禁：抽取两个页面全部内联 <script>，逐个 node --check。
// 用法：node scripts/check_ui.js   （CI/重构验收：exit 0 = 无语法错误）
const fs = require("fs");
const path = require("path");
const os = require("os");
const cp = require("child_process");

const pages = [
  path.join(__dirname, "..", "myjev", "web", "index.html"),
  path.join(__dirname, "..", "myjev", "app", "index.html"),
];
let fail = 0;
for (const p of pages) {
  if (!fs.existsSync(p)) { console.log("SKIP(缺失)", p); continue; }
  const html = fs.readFileSync(p, "utf8");
  const blocks = [...html.matchAll(/<script(?![^>]*src)[^>]*>([\s\S]*?)<\/script>/g)];
  blocks.forEach((m, i) => {
    const tmp = path.join(os.tmpdir(), `jev_ui_${path.basename(p)}_${i}.js`);
    fs.writeFileSync(tmp, m[1]);
    const r = cp.spawnSync(process.execPath, ["--check", tmp], { encoding: "utf8" });
    if (r.status === 0) console.log("OK  ", path.basename(p), `block#${i}`);
    else { fail = 1; console.log("FAIL", path.basename(p), `block#${i}\n` + r.stderr.slice(0, 800)); }
    fs.rmSync(tmp, { force: true });
  });
}
process.exit(fail);
