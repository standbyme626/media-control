@echo off
start "" "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" ^
  --remote-debugging-port=9222 ^
  --remote-allow-origins=* ^
  --user-data-dir=%USERPROFILE%\edge-music ^
  --no-first-run --no-default-browser-check ^
  --new-window "https://www.bilibili.com/"
