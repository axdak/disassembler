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
import { spawn, execSync } from 'node:child_process'

const isWin = process.platform === 'win32'
const passthrough = process.argv.slice(2)

// Must match renderer.server.port in electron.vite.config.ts (strictPort).
const DEV_PORT = 5173

// Kill whatever is holding DEV_PORT so `strictPort` doesn't abort the
// launch. 5173 is this project's strictPort-exclusive port, so any holder
// is treated as stale and killed -- including processes that aren't this
// project's own dev.
function freePortIfStaleDev() {
  if (!isWin) return
  let pid
  try {
    const out = execSync(
      `powershell -NoProfile -Command "(Get-NetTCPConnection -LocalPort ${DEV_PORT} -State Listen -ErrorAction SilentlyContinue).OwningProcess"`,
      { encoding: 'utf8' }
    )
    pid = parseInt(out.trim(), 10)
  } catch { return }
  if (!pid || Number.isNaN(pid)) return
  process.stderr.write(`[dev] killing stale process ${pid} on port ${DEV_PORT}\n`)
  try { execSync(`taskkill /F /T /PID ${pid}`, { stdio: 'ignore' }) } catch { /* ignore */ }
}

function runVite() {
  freePortIfStaleDev()
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
