' Lanceur sans fenêtre console pour Plume (dictée vocale).
' Double-cliquez ce fichier pour démarrer « Plume » proprement.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
pyw = dir & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(pyw) Then
    MsgBox "Plume n'est pas encore installé : lancez d'abord install.bat (double-clic)." & vbCrLf & vbCrLf & _
           "Fichier attendu : " & pyw, vbExclamation, "Plume"
    WScript.Quit 1
End If
' pythonw.exe = interpréteur Python sans console ; 0 = fenêtre du lanceur masquée.
sh.Run """" & pyw & """ """ & dir & "\plume.py""", 0, False
