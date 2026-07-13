@echo off
REM 局域网访问：在其它设备浏览器打开 http://<本机IP>:8765
REM 首次使用若连不上，请在 Windows 防火墙放行 8765 端口（专用网络）。
python .\editor\server.py --data-dir ..\..\data\plugins\maibot_plugin.keywords_reply --host 0.0.0.0 --port 8765
