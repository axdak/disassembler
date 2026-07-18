// Cross-platform dev launcher.
//
// On Windows, set the console code page to UTF-8 (65001) before running
// electron-vite dev. The default code page on a Chinese Windows console is
// 936 (GBK), which turns Chinese stdout/stderr from the backend subprocess
// into mojibake (U+FFFD / '?') before Electron ever decodes it. `chcp 65001`
// plus PYTHONUTF8 (set on the spawn env) plus the StringDecoder in
// backend-process.ts together close the mojibake loop.
//
// On non-Windows, just run electron-vite dev directly. CI / typecheck / build
// use `npm run dev:direct` (always electron-vite dev) or the unchanged
// `build`/`typecheck:*` scripts.
import { spawn } from 'node:child_process'

const isWin = process.platform === 'win32'
const passthrough = process.argv.slice(2)

function runVite() {
  const child = spawn('electron-vite', ['dev', ...passthrough], {
    stdio: 'inherit',
    shell: true,
  })
  child.on('exit', (code) => process.exit(code ?? 0))
}

if (isWin) {
  const chcp = spawn('chcp 65001 > NUL', { shell: true, stdio: 'inherit' })
  chcp.on('exit', (code) => {
    // `chcp` returns non-zero only if the console can't switch; ignore and
    // still launch — PYTHONUTF8 + StringDecoder keep streams clean regardless.
    runVite()
  })
  chcp.on('error', () => runVite())
} else {
  runVite()
}
