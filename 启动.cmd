@echo off
setlocal

rem Keep Chinese status messages readable in the Windows console.
chcp 65001 >nul 2>&1
title Novel Disassembler Desktop

rem Resolve all paths relative to this script so double-clicking works from any current directory.
if not exist "%~dp0desktop\package.json" goto missing_desktop

rem Fail early with an actionable message when the JavaScript runtime is unavailable.
where node >nul 2>&1
if errorlevel 1 goto missing_node

where npm >nul 2>&1
if errorlevel 1 goto missing_npm

pushd "%~dp0desktop" >nul 2>&1
if errorlevel 1 goto missing_desktop

rem A leftover node_modules directory can be incomplete after cleanup or a failed install.
rem Check the actual launch files; a healthy installation still starts without reinstalling.
call :check_dependencies
if errorlevel 1 goto install_dependencies
goto start_application

:install_dependencies
echo [启动] 启动依赖缺失或不完整，正在恢复，请稍候...
rem Prefer the committed lockfile so recovery does not silently upgrade dependencies.
if exist "package-lock.json" goto install_locked_dependencies
call npm install
goto check_install_result

:install_locked_dependencies
call npm ci

:check_install_result
if errorlevel 1 goto install_failed
call :check_dependencies
if errorlevel 1 goto incomplete_dependencies

:start_application
echo [启动] 正在启动 Novel Disassembler Desktop...
call npm run dev
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" goto start_failed

popd
exit /b 0

:install_failed
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo [错误] 依赖安装失败，请检查网络连接及上方错误信息。
goto fail_after_pushd

:incomplete_dependencies
set "EXIT_CODE=1"
echo.
echo [错误] 安装后启动文件仍不完整，请检查 Electron 下载是否成功及上方安装日志。
goto fail_after_pushd

:start_failed
echo.
echo [错误] 应用启动失败，退出代码：%EXIT_CODE%。

:fail_after_pushd
popd
goto wait_and_exit

:missing_desktop
set "EXIT_CODE=1"
echo [错误] 未找到 desktop\package.json，请将本脚本放在项目根目录后重试。
goto wait_and_exit

:missing_node
set "EXIT_CODE=1"
echo [错误] 未检测到 Node.js，请先安装 Node.js 并重新打开本脚本。
goto wait_and_exit

:missing_npm
set "EXIT_CODE=1"
echo [错误] 未检测到 npm，请修复 Node.js 安装后重试。

:wait_and_exit
echo.
echo 按任意键关闭窗口...
pause >nul
exit /b %EXIT_CODE%

:check_dependencies
rem Verify both the Windows command shim and its real entry point, plus Electron itself.
if not exist "node_modules\.bin\electron-vite.cmd" exit /b 1
if not exist "node_modules\electron-vite\bin\electron-vite.js" exit /b 1
if not exist "node_modules\electron\dist\electron.exe" exit /b 1
exit /b 0
