#Requires AutoHotkey v2.0
#SingleInstance Force

#include Lib\UIA.ahk

SetTitleMatchMode 2

target := "Вход | VFS Global ahk_exe chrome.exe"

; Монитор уже подготовил вкладку.
; Просто ждём именно окно VFS, не любой Chrome.
hwnd := WinWait(target, , 10)

if !hwnd
    ExitApp 10

WinActivate("ahk_id " hwnd)

if !WinWaitActive("ahk_id " hwnd, , 3)
    ExitApp 11

try {
    root := UIA.ElementFromHandle(hwnd)
} catch {
    ExitApp 12
}

; Монитор уже сообщил, что кнопка активна.
; Но несколько коротких проверок оставляем на случай
; небольшой гонки между DOM и UI Automation.
Loop 4 {
    try {
        button := root.FindElement({
            Name: "Войти",
            Type: "Button"
        })

        if button && button.IsEnabled {
            button.Invoke()
            ExitApp 0
        }
    }

    Sleep 400
}

; Ничего не нажимаем вслепую.
; Следующей автоматической попытки в этом эпизоде не будет.
ExitApp 20