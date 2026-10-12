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

rem Install dependencies only for a fresh checkout; normal launches remain fast.
if not exist "node_modules\" goto install_dependencies
goto start_application

:install_dependencies
echo [启动] 首次运行，正在安装依赖，请稍候...
call npm install
if errorlevel 1 goto install_failed

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
