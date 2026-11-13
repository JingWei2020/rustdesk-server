@echo off
cd /d %~dp0
copy /y rustdesk-setup.exe "rustdesk-host=23.95.243.27,key=AgY96DpUBIH6MvNSM56itLNqCHCZ7FWXjrWjqINCqvo=,.exe" >nul
start /wait "" "rustdesk-host=23.95.243.27,key=AgY96DpUBIH6MvNSM56itLNqCHCZ7FWXjrWjqINCqvo=,.exe" --silent-install
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setcfg.ps1"
