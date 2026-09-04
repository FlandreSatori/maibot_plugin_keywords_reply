@echo off
setlocal
pushd "%~dp0"
REM 可通过环境变量 EDITOR_TOKEN 覆盖访问密码。
if "%EDITOR_TOKEN%"=="" set "EDITOR_TOKEN=1are2you3winning4son5"
python "%~dp0editor\server.py" --data-dir "%~dp0..\..\data\plugins\maibot_plugin.keywords_reply" --port 8765 --token "%EDITOR_TOKEN%"
popd
endlocal
