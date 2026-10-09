/**
 * Google Apps Script Web App for Google Sheets MCP Server
 * Supports:
 * - doGet: Read sheets, fetch jobs or mails as JSON
 * - doPost: Entry (Insert), Update, Delete (Strictly 1 at a time), and backward-compatible Inquiry submission to "Recent Mails"
 */

function doGet(e) {
  try {
    var id = (e && e.parameter && e.parameter.id) ? e.parameter.id : null;
    if (!id) {
      return ContentService
        .createTextOutput(JSON.stringify({ error: "Missing Sheet ID (?id=...)" }))
        .setMimeType(ContentService.MimeType.JSON);
    }

    var ss = SpreadsheetApp.openById(id);
    
    // Optional schema inspection (?action=schema)
    if (e.parameter.action === "schema") {
      var sheets = ss.getSheets();
      var schema = {};
      for (var s = 0; s < sheets.length; s++) {
        var sName = sheets[s].getName();
        var numCols = Math.max(sheets[s].getLastColumn(), 1);
        var sHeaders = sheets[s].getRange(1, 1, 1, numCols).getDisplayValues()[0];
        schema[sName] = sHeaders.map(function(h) { return String(h).trim(); }).filter(Boolean);
      }
      return ContentService
        .createTextOutput(JSON.stringify({ success: true, schema: schema }))
        .setMimeType(ContentService.MimeType.JSON);
    }

    // Looks for "Recent Jobs" tab first, or a ?sheet= parameter, or the 1st sheet tab
    var sheet = (e.parameter.sheet ? ss.getSheetByName(e.parameter.sheet) : null)
             || ss.getSheetByName("Recent Jobs") 
             || ss.getSheets()[0];

    var data = sheet.getDataRange().getDisplayValues();

    if (!data || data.length < 2) {
      return ContentService
        .createTextOutput(JSON.stringify([]))
        .setMimeType(ContentService.MimeType.JSON);
    }

    // Clean and trim header names
    var headers = data[0].map(function(h) {
      return String(h).trim();
    });

    var rows = [];

    for (var i = 1; i < data.length; i++) {
      var row = data[i];
      
      // Skip empty rows
      var hasContent = row.some(function(cell) { return cell.trim() !== ""; });
      if (!hasContent) continue;

      var obj = {};
      for (var j = 0; j < headers.length; j++) {
        if (headers[j]) {
          obj[headers[j]] = row[j];
        }
      }
      rows.push(obj);
    }

    return ContentService
      .createTextOutput(JSON.stringify(rows))
      .setMimeType(ContentService.MimeType.JSON);

  } catch (err) {
    return ContentService
      .createTextOutput(JSON.stringify({ error: err.toString() }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}


function doPost(e) {
  // Use a lock to prevent concurrent write conflicts
  var lock = LockService.getScriptLock();
  lock.tryLock(15000);

  try {
    var data = {};
    if (e.postData && e.postData.contents) {
      try {
        data = JSON.parse(e.postData.contents);
      } catch (parseErr) {
        data = e.parameter || {};
      }
    } else if (e.parameter) {
      data = e.parameter;
    }
    
    // Get Sheet ID from query param (?id=...) OR from POST body
    var id = (e && e.parameter && e.parameter.id) || data.sheetId;
    
    if (!id) {
      return ContentService
        .createTextOutput(JSON.stringify({ success: false, error: "Missing Sheet ID" }))
        .setMimeType(ContentService.MimeType.JSON);
    }
    
    var ss = SpreadsheetApp.openById(id);
    var action = (data.action || '').toString().toLowerCase().trim();

    // ==============================================================
    // 1. DELETE ACTION (Strictly 1 record at a time to prevent loss)
    // ==============================================================
    if (action === "delete") {
      var targetSheetName = data.sheet || "Recent Jobs";
      var sheet = ss.getSheetByName(targetSheetName);
      if (!sheet) {
        return ContentService
          .createTextOutput(JSON.stringify({ success: false, error: "Sheet '" + targetSheetName + "' not found" }))
          .setMimeType(ContentService.MimeType.JSON);
      }

      var matchCol = (data.identifierColumn || "slug").toString().trim();
      var matchVal = (data.identifierValue || data.slug || "").toString().trim();

      if (!matchVal) {
        return ContentService
          .createTextOutput(JSON.stringify({ success: false, error: "Missing identifierValue / slug for deletion" }))
          .setMimeType(ContentService.MimeType.JSON);
      }

      var dataRange = sheet.getDataRange().getDisplayValues();
      if (!dataRange || dataRange.length < 2) {
        return ContentService
          .createTextOutput(JSON.stringify({ success: false, error: "Sheet is empty or has no data" }))
          .setMimeType(ContentService.MimeType.JSON);
      }

      var headers = dataRange[0].map(function(h) { return String(h).trim(); });
      var targetColIdx = -1;
      for (var c = 0; c < headers.length; c++) {
        if (headers[c].toLowerCase() === matchCol.toLowerCase()) {
          targetColIdx = c;
          break;
        }
      }

      if (targetColIdx === -1) {
        return ContentService
          .createTextOutput(JSON.stringify({ success: false, error: "Column '" + matchCol + "' not found in sheet" }))
          .setMimeType(ContentService.MimeType.JSON);
      }

      var matchingRowIndices = [];
      var deletedItem = null;

      for (var r = 1; r < dataRange.length; r++) {
        if (dataRange[r][targetColIdx].toString().trim() === matchVal) {
          matchingRowIndices.push(r + 1); // 1-based index in sheet
          var item = {};
          for (var j = 0; j < headers.length; j++) {
            item[headers[j]] = dataRange[r][j];
          }
          deletedItem = item;
        }
      }

      if (matchingRowIndices.length === 0) {
        return ContentService
          .createTextOutput(JSON.stringify({ success: false, error: "No record found matching " + matchCol + " = '" + matchVal + "'" }))
          .setMimeType(ContentService.MimeType.JSON);
      }

      // STRICT SAFETY CONSTRAINT: Delete at most 1 item!
      if (matchingRowIndices.length > 1) {
        return ContentService
          .createTextOutput(JSON.stringify({ 
            success: false, 
            error: "Safety violation: Multiple records (" + matchingRowIndices.length + ") match '" + matchVal + "'. Deletions are strictly restricted to 1 job at a time."
          }))
          .setMimeType(ContentService.MimeType.JSON);
      }

      // Delete the exact row
      sheet.deleteRow(matchingRowIndices[0]);

      return ContentService
        .createTextOutput(JSON.stringify({ 
          success: true, 
          message: "Job deleted successfully", 
          deletedJob: deletedItem,
          rowDeleted: matchingRowIndices[0]
        }))
        .setMimeType(ContentService.MimeType.JSON);
    }

    // ==============================================================
    // 2. UPDATE ACTION
    // ==============================================================
    if (action === "update") {
      var targetSheetName = data.sheet || "Recent Jobs";
      var sheet = ss.getSheetByName(targetSheetName);
      if (!sheet) {
        return ContentService
          .createTextOutput(JSON.stringify({ success: false, error: "Sheet '" + targetSheetName + "' not found" }))
          .setMimeType(ContentService.MimeType.JSON);
      }

      var matchCol = (data.identifierColumn || "slug").toString().trim();
      var matchVal = (data.identifierValue || data.slug || "").toString().trim();
      var updates = data.updates || {};

      if (!matchVal) {
        return ContentService
          .createTextOutput(JSON.stringify({ success: false, error: "Missing identifierValue / slug for update" }))
          .setMimeType(ContentService.MimeType.JSON);
      }

      var dataRange = sheet.getDataRange().getDisplayValues();
      var headers = dataRange[0].map(function(h) { return String(h).trim(); });
      var targetColIdx = -1;
      for (var c = 0; c < headers.length; c++) {
        if (headers[c].toLowerCase() === matchCol.toLowerCase()) {
          targetColIdx = c;
          break;
        }
      }

      if (targetColIdx === -1) {
        return ContentService
          .createTextOutput(JSON.stringify({ success: false, error: "Column '" + matchCol + "' not found" }))
          .setMimeType(ContentService.MimeType.JSON);
      }

      var updatedCount = 0;
      var updatedRows = [];

      for (var r = 1; r < dataRange.length; r++) {
        if (dataRange[r][targetColIdx].toString().trim() === matchVal) {
          var rowNum = r + 1;
          for (var updKey in updates) {
            for (var c = 0; c < headers.length; c++) {
              if (headers[c].toLowerCase() === updKey.toLowerCase()) {
                sheet.getRange(rowNum, c + 1).setValue(updates[updKey]);
                break;
              }
            }
          }
          updatedCount++;
          updatedRows.push(rowNum);
        }
      }

      if (updatedCount === 0) {
        return ContentService
          .createTextOutput(JSON.stringify({ success: false, error: "No record found matching " + matchCol + " = '" + matchVal + "'" }))
          .setMimeType(ContentService.MimeType.JSON);
      }

      return ContentService
        .createTextOutput(JSON.stringify({ 
          success: true, 
          message: "Job updated successfully", 
          updatedCount: updatedCount,
          updatedRows: updatedRows,
          updates: updates
        }))
        .setMimeType(ContentService.MimeType.JSON);
    }

    // ==============================================================
    // 3. INSERT / ENTRY ACTION (Add Job to Recent Jobs)
    // ==============================================================
    if (action === "insert" || action === "entry") {
      var targetSheetName = data.sheet || "Recent Jobs";
      var sheet = ss.getSheetByName(targetSheetName) || ss.insertSheet(targetSheetName);
      var rowObj = data.row || data.job || data;

      var lastCol = Math.max(sheet.getLastColumn(), 1);
      var headers = sheet.getRange(1, 1, 1, lastCol).getValues()[0];

      var rowValues = [];
      for (var c = 0; c < headers.length; c++) {
        var hName = (headers[c] || '').toString().trim();
        var val = '';
        for (var k in rowObj) {
          if (k.toLowerCase() === hName.toLowerCase()) {
            val = rowObj[k];
            break;
          }
        }
        rowValues.push(val);
      }

      sheet.appendRow(rowValues);

      return ContentService
        .createTextOutput(JSON.stringify({ 
          success: true, 
          message: "Job added to " + targetSheetName, 
          row: sheet.getLastRow(),
          job: rowObj
        }))
        .setMimeType(ContentService.MimeType.JSON);
    }

    // ==============================================================
    // 4. DEFAULT FALLBACK: Recent Mails Form Submission (Unchanged)
    // ==============================================================
    var sheet = ss.getSheetByName("Recent Mails") || ss.insertSheet("Recent Mails");
    
    var timestamp = data.timestamp || Utilities.formatDate(new Date(), "Asia/Kolkata", "dd/MM/yyyy, hh:mm:ss a");
    var name = (data.name || '').toString().trim();
    var email = (data.email || '').toString().trim();
    var phone = (data.phone || '').toString().trim();
    var city = (data.city || '').toString().trim();
    var queryType = (data.queryType || data.projectType || 'General Inquiry').toString().trim();
    var jobName = (data.jobName || data.budget || '-').toString().trim();
    var message = (data.message || '').toString().trim();

    var defaultHeaders = ["Timestamp", "Name", "Email", "Phone", "City", "Query Type", "Job Name", "Message"];
    
    if (sheet.getLastRow() === 0) {
      sheet.appendRow(defaultHeaders);
      var headerRange = sheet.getRange(1, 1, 1, defaultHeaders.length);
      headerRange.setFontWeight("bold");
      headerRange.setBackground("#1e293b");
      headerRange.setFontColor("#ffffff");
      sheet.setFrozenRows(1);
    }
    
    var lastCol = Math.max(sheet.getLastColumn(), 1);
    var existingHeaders = sheet.getRange(1, 1, 1, lastCol).getValues()[0];
    
    var rowValues = [];
    var mappedAny = false;
    
    if (existingHeaders && existingHeaders.length > 0 && existingHeaders[0] !== "") {
      for (var i = 0; i < existingHeaders.length; i++) {
        var h = (existingHeaders[i] || '').toString().toLowerCase().trim();
        
        if (h.indexOf("time") !== -1 || h.indexOf("date") !== -1) {
          rowValues.push(timestamp);
          mappedAny = true;
        } else if (h.indexOf("job") !== -1 || h.indexOf("role") !== -1 || h.indexOf("budget") !== -1 || h.indexOf("designation") !== -1) {
          rowValues.push(jobName);
          mappedAny = true;
        } else if (h.indexOf("query") !== -1 || h.indexOf("type") !== -1 || h.indexOf("project") !== -1) {
          rowValues.push(queryType);
          mappedAny = true;
        } else if (h.indexOf("name") !== -1) {
          rowValues.push(name);
          mappedAny = true;
        } else if (h.indexOf("email") !== -1 || h.indexOf("mail") !== -1) {
          rowValues.push(email);
          mappedAny = true;
        } else if (h.indexOf("phone") !== -1 || h.indexOf("mobile") !== -1 || h.indexOf("contact") !== -1) {
          rowValues.push(phone);
          mappedAny = true;
        } else if (h.indexOf("city") !== -1 || h.indexOf("location") !== -1) {
          rowValues.push(city);
          mappedAny = true;
        } else if (h.indexOf("message") !== -1 || h.indexOf("detail") !== -1 || h.indexOf("note") !== -1 || h.indexOf("desc") !== -1) {
          rowValues.push(message);
          mappedAny = true;
        } else {
          rowValues.push('');
        }
      }
    }
    
    if (!mappedAny || rowValues.length === 0) {
      rowValues = [timestamp, name, email, phone, city, queryType, jobName, message];
    }
    
    sheet.appendRow(rowValues);
    
    return ContentService
      .createTextOutput(JSON.stringify({ 
        success: true, 
        message: "Saved to Recent Mails",
        row: sheet.getLastRow()
      }))
      .setMimeType(ContentService.MimeType.JSON);

  } catch (err) {
    return ContentService
      .createTextOutput(JSON.stringify({ success: false, error: err.toString() }))
      .setMimeType(ContentService.MimeType.JSON);
  } finally {
    lock.releaseLock();
  }
}
