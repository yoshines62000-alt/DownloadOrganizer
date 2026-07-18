' Lance l'interface graphique du Nettoyeur intelligent sans ouvrir de console.
' Double-cliquez simplement sur ce fichier pour demarrer l'application.

Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

Set shell = CreateObject("WScript.Shell")
command = "pythonw.exe """ & scriptDir & "\gui.py"""
shell.CurrentDirectory = scriptDir

On Error Resume Next
shell.Run command, 0, False
If Err.Number <> 0 Then
    MsgBox "Impossible de lancer l'application." & vbCrLf & _
           "Verifiez que Python est installe et accessible (pythonw.exe)." & vbCrLf & vbCrLf & _
           "Detail : " & Err.Description, vbExclamation, "Nettoyeur intelligent"
End If
On Error Goto 0
