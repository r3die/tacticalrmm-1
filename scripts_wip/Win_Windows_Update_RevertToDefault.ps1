# Tactical RMM Patch management disables Windows Automatic Update settings by setting the registry key below to 1. 
# Run this to revert back to default

Set-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" -Name "AUOptions" -Value "0"