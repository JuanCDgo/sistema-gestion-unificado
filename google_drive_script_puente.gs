/**
 * PUENTE DIRECTO A GOOGLE DRIVE PARA BACKUP AUTOMATICO DE CARPETA "datos"
 * -----------------------------------------------------------------------
 * Cómo activarlo gratis en tu propia cuenta de Google Drive (lleva 2 minutos):
 *
 * 1. Entrá desde el navegador a: https://script.google.com
 * 2. Tocá "Nuevo proyecto".
 * 3. Borrá lo que haya en pantalla y pegá todo este código.
 * 4. Arriba a la derecha tocá "Implementar" -> "Nueva implementación".
 * 5. En "Seleccionar tipo" (el engranaje) elegí: "Aplicación web".
 *    - Ejecutar como: "Yo" (tu cuenta de Google).
 *    - Quién tiene acceso: "Cualquier persona".
 * 6. Tocá "Implementar", autorizá el acceso a tu Google Drive y copiá la
 *    "URL de la aplicación web" (termina en /exec).
 * 7. Pegá esa URL en la pestaña Configuración -> Backup Automático en Google Drive
 *    dentro de la app del celular.
 *
 * Resultado: cada vez que el celular se conecte al Wi-Fi seguro de tu casa,
 * subirá solo el archivo ZIP de la carpeta "datos" adentro de tu carpeta
 * designada en Google Drive.
 */

function doPost(e) {
  try {
    var data = JSON.parse(e.postData.contents);
    var TARGET_FOLDER_ID = "1N42I4cxQdPnM7B8UoZnPSsr5tD2Q0pS1";
    var folder;
    var folderName = "Backups_POS_Celular";

    try {
      folder = DriveApp.getFolderById(TARGET_FOLDER_ID);
      folderName = folder.getName();
    } catch (_) {
      var folders = DriveApp.getFoldersByName(folderName);
      folder = folders.hasNext() ? folders.next() : DriveApp.createFolder(folderName);
    }

    var bytes = Utilities.base64Decode(data.fileBase64);
    var blob = Utilities.newBlob(bytes, data.mimeType || "application/zip", data.filename || "datos_backup.zip");

    // 1. Guardar copia con fecha y hora
    var file = folder.createFile(blob);

    // 2. Actualizar también el archivo "ACTUAL" para tener siempre a mano el último
    var latestName = data.latest_filename || "datos_backup_ACTUAL.zip";
    var existing = folder.getFilesByName(latestName);
    while (existing.hasNext()) {
      existing.next().setTrashed(true);
    }
    var blobLatest = Utilities.newBlob(bytes, data.mimeType || "application/zip", latestName);
    folder.createFile(blobLatest);

    // 3. Mantener solo los últimos 15 backups con fecha en Google Drive para no llenar espacio
    var allFiles = folder.getFiles();
    var datedFiles = [];
    while (allFiles.hasNext()) {
      var f = allFiles.next();
      if (f.getName() !== latestName) {
        datedFiles.push(f);
      }
    }
    datedFiles.sort(function(a, b) {
      return b.getDateCreated().getTime() - a.getDateCreated().getTime();
    });
    for (var i = 15; i < datedFiles.length; i++) {
      datedFiles[i].setTrashed(true);
    }

    return ContentService.createTextOutput(JSON.stringify({
      ok: true,
      message: "Guardado en Google Drive / " + folderName + " (" + file.getName() + ")"
    })).setMimeType(ContentService.MimeType.JSON);

  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify({
      ok: false,
      error: err.toString()
    })).setMimeType(ContentService.MimeType.JSON);
  }
}
