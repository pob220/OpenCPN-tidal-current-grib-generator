#include "currentgrib_dialog.h"

#include <wx/datetime.h>
#include <wx/filedlg.h>
#include <wx/filename.h>
#include <wx/msgdlg.h>
#include <wx/process.h>
#include <wx/scrolwin.h>
#include <wx/stdpaths.h>
#include <wx/stream.h>
#include <wx/utils.h>

namespace {

wxString ShellQuote(const wxString& value) {
  wxString escaped(value);
  escaped.Replace("'", "'\\''");
  return "'" + escaped + "'";
}

wxString DefaultOutputDirectory() {
  wxFileName path(wxStandardPaths::Get().GetUserDataDir(), "");
  path.AppendDir("grib");
  path.AppendDir("generated");
  return path.GetPath();
}

wxString DefaultOutputFilename() {
  return "current_grib_" + wxDateTime::Now().ToUTC().Format("%Y%m%d_%H%M") + ".grb";
}

wxString DefaultStartUtc() {
  wxDateTime now = wxDateTime::Now().ToUTC();
  now.SetMinute(0);
  now.SetSecond(0);
  now.SetMillisecond(0);
  return now.FormatISOCombined('T') + "Z";
}

wxString JsonEscape(const wxString& value) {
  wxString escaped;
  for (wxUniChar ch : value) {
    if (ch == '\\') {
      escaped += "\\\\";
    } else if (ch == '"') {
      escaped += "\\\"";
    } else if (ch == '\n') {
      escaped += "\\n";
    } else if (ch == '\r') {
      escaped += "\\r";
    } else if (ch == '\t') {
      escaped += "\\t";
    } else {
      escaped += ch;
    }
  }
  return escaped;
}

bool IsExecutableFile(const wxString& path) {
  return wxFileName::FileExists(path);
}

void RedactQueryParameter(wxString* text, const wxString& name) {
  wxString lower = text->Lower();
  wxString needle1 = "?" + name.Lower() + "=";
  wxString needle2 = "&" + name.Lower() + "=";
  for (const auto& needle : {needle1, needle2}) {
    size_t position = lower.find(needle);
    while (position != wxString::npos) {
      size_t value_start = position + needle.Length();
      size_t value_end = value_start;
      while (value_end < text->Length() &&
             (*text)[value_end] != '&' && (*text)[value_end] != '#' &&
             !wxIsspace((*text)[value_end])) {
        ++value_end;
      }
      text->replace(value_start, value_end - value_start, "<redacted>");
      lower = text->Lower();
      position = lower.find(needle, value_start + 10);
    }
  }
}

}  // namespace

CurrentGribDialog::CurrentGribDialog(wxWindow* parent)
    : wxDialog(parent, wxID_ANY, "Ocean Current GRIB Generator", wxDefaultPosition,
               wxSize(880, 760), wxDEFAULT_DIALOG_STYLE | wxRESIZE_BORDER) {
  auto* top = new wxBoxSizer(wxVERTICAL);
  auto* scrolled = new wxScrolledWindow(this, wxID_ANY, wxDefaultPosition, wxDefaultSize,
                                       wxVSCROLL | wxTAB_TRAVERSAL);
  scrolled->SetScrollRate(8, 8);
  scrolled->SetMinSize(wxSize(760, 330));
  auto* form = new wxBoxSizer(wxVERTICAL);
  auto* grid = new wxFlexGridSizer(2, 8, 8);
  grid->AddGrowableCol(1, 1);

  m_generatorPath = new wxTextCtrl(scrolled, wxID_ANY, FindDefaultGenerator());
  m_west = new wxTextCtrl(scrolled, wxID_ANY, "-8.5");
  m_south = new wxTextCtrl(scrolled, wxID_ANY, "50.5");
  m_east = new wxTextCtrl(scrolled, wxID_ANY, "-2.5");
  m_north = new wxTextCtrl(scrolled, wxID_ANY, "56.5");
  wxString presets[] = {"Custom bbox", "Current chart area",
                        "Irish Sea / North Channel example", "Tiny Copernicus connection test"};
  m_presetChoice = new wxChoice(scrolled, wxID_ANY, wxDefaultPosition, wxDefaultSize, WXSIZEOF(presets), presets);
  m_presetChoice->SetSelection(0);
  m_startUtc = new wxTextCtrl(scrolled, wxID_ANY, DefaultStartUtc());
  m_durationHours = new wxSpinCtrl(scrolled, wxID_ANY);
  m_durationHours->SetRange(1, 240);
  m_durationHours->SetValue(72);
  m_stepHours = new wxSpinCtrl(scrolled, wxID_ANY);
  m_stepHours->SetRange(1, 24);
  m_stepHours->SetValue(1);

  wxString providers[] = {"Auto", "Copernicus Marine North-West Shelf high-resolution currents",
                          "Local NetCDF file", "Synthetic test source"};
  m_provider = new wxChoice(scrolled, wxID_ANY, wxDefaultPosition, wxDefaultSize, WXSIZEOF(providers), providers);
  m_provider->SetSelection(1);
  m_username = new wxTextCtrl(scrolled, wxID_ANY);
  m_password = new wxTextCtrl(scrolled, wxID_ANY, "", wxDefaultPosition, wxDefaultSize, wxTE_PASSWORD);
  m_rememberUsername = new wxCheckBox(scrolled, wxID_ANY, "Remember username");
  m_localNetcdf = new wxFilePickerCtrl(scrolled, wxID_ANY, "", "Select NetCDF file", "*.nc;*.nc4");
  m_outputDir = new wxDirPickerCtrl(scrolled, wxID_ANY, DefaultOutputDirectory());
  m_outputFile = new wxTextCtrl(scrolled, wxID_ANY, DefaultOutputFilename());
  auto* outputBrowse = new wxButton(scrolled, wxID_ANY, "Browse...");
  m_openAfter = new wxCheckBox(scrolled, wxID_ANY, "Open generated current GRIB after creation");
  m_showMergeInstructions = new wxCheckBox(scrolled, wxID_ANY, "Show instructions for merging with weather GRIB");
  m_showMergeInstructions->SetValue(true);

  auto addRow = [&](const wxString& label, wxWindow* control) {
    grid->Add(new wxStaticText(scrolled, wxID_ANY, label), 0, wxALIGN_CENTER_VERTICAL);
    grid->Add(control, 1, wxEXPAND);
  };
  addRow("Generator executable", m_generatorPath);
  addRow("West longitude", m_west);
  addRow("South latitude", m_south);
  addRow("East longitude", m_east);
  addRow("North latitude", m_north);
  addRow("Area preset", m_presetChoice);
  addRow("Start UTC", m_startUtc);
  addRow("Duration hours", m_durationHours);
  addRow("Step hours", m_stepHours);
  addRow("Data source", m_provider);
  addRow("Copernicus username", m_username);
  addRow("Copernicus password", m_password);
  addRow("Local NetCDF", m_localNetcdf);
  addRow("Output directory", m_outputDir);
  auto* outputFileSizer = new wxBoxSizer(wxHORIZONTAL);
  outputFileSizer->Add(m_outputFile, 1, wxEXPAND | wxRIGHT, 8);
  outputFileSizer->Add(outputBrowse, 0);
  grid->Add(new wxStaticText(scrolled, wxID_ANY, "Output filename"), 0, wxALIGN_CENTER_VERTICAL);
  grid->Add(outputFileSizer, 1, wxEXPAND);

  form->Add(grid, 0, wxEXPAND | wxALL, 12);
  form->Add(m_rememberUsername, 0, wxLEFT | wxRIGHT | wxBOTTOM, 12);
  form->Add(m_openAfter, 0, wxLEFT | wxRIGHT | wxBOTTOM, 12);
  form->Add(m_showMergeInstructions, 0, wxLEFT | wxRIGHT | wxBOTTOM, 12);
  scrolled->SetSizer(form);
  top->Add(scrolled, 1, wxEXPAND);

  m_log = new wxTextCtrl(this, wxID_ANY, "", wxDefaultPosition, wxSize(-1, 220),
                         wxTE_MULTILINE | wxTE_READONLY | wxTE_DONTWRAP);
  m_log->SetMinSize(wxSize(760, 180));
  top->Add(m_log, 0, wxEXPAND | wxLEFT | wxRIGHT | wxTOP | wxBOTTOM, 12);

  auto* buttons = new wxBoxSizer(wxHORIZONTAL);
  m_checkButton = new wxButton(this, wxID_ANY, "Check Dependencies");
  m_generateButton = new wxButton(this, wxID_OK, "Generate Current GRIB");
  m_cancelButton = new wxButton(this, wxID_ANY, "Cancel");
  m_closeButton = new wxButton(this, wxID_CANCEL, "Close");
  buttons->Add(m_checkButton, 0, wxRIGHT, 8);
  buttons->AddStretchSpacer(1);
  buttons->Add(m_generateButton, 0, wxRIGHT, 8);
  buttons->Add(m_cancelButton, 0, wxRIGHT, 8);
  buttons->Add(m_closeButton, 0);
  top->Add(buttons, 0, wxEXPAND | wxLEFT | wxRIGHT | wxBOTTOM, 12);

  SetSizerAndFit(top);
  SetMinSize(wxSize(880, 720));
  SetSize(wxSize(900, 780));
  CentreOnParent();

  m_checkButton->Bind(wxEVT_BUTTON, &CurrentGribDialog::OnCheckDependencies, this);
  m_generateButton->Bind(wxEVT_BUTTON, &CurrentGribDialog::OnGenerate, this);
  outputBrowse->Bind(wxEVT_BUTTON, &CurrentGribDialog::OnBrowseOutput, this);
  m_presetChoice->Bind(wxEVT_CHOICE, &CurrentGribDialog::OnPresetChanged, this);
  m_cancelButton->Bind(wxEVT_BUTTON, &CurrentGribDialog::OnCancel, this);
  m_closeButton->Bind(wxEVT_BUTTON, &CurrentGribDialog::OnClose, this);
  Bind(wxEVT_CLOSE_WINDOW, &CurrentGribDialog::OnDialogClose, this);
  Bind(wxEVT_TIMER, &CurrentGribDialog::OnProcessTimer, this);
  Bind(wxEVT_END_PROCESS, &CurrentGribDialog::OnProcessTerminated, this);

  AppendLog("Generated current GRIBs are model data for planning and experimentation, not official navigation products.");
  SetBusy(false);
}

CurrentGribDialog::~CurrentGribDialog() {
  m_processTimer.Stop();
  if (m_process) {
    m_process->Detach();
    m_process = nullptr;
  }
}

void CurrentGribDialog::SetCurrentViewPort(const PlugIn_ViewPort& vp) {
  m_currentViewPort = vp;
  m_hasCurrentViewPort = vp.bValid;
}

void CurrentGribDialog::OnCheckDependencies(wxCommandEvent&) {
  wxString command = ShellQuote(m_generatorPath->GetValue()) + " check-dependencies --output-directory " +
                     ShellQuote(m_outputDir->GetPath());
  AppendLog("Running dependency check...");
  StartCommand(command, "", false);
}

void CurrentGribDialog::OnGenerate(wxCommandEvent&) {
  wxString provider = m_provider->GetStringSelection();
  if (provider.Contains("Local NetCDF") && m_localNetcdf->GetPath().empty()) {
    wxString message = "Select a local NetCDF file before generating a current GRIB.";
    AppendLog(message);
    wxMessageBox(message, "Missing NetCDF file", wxOK | wxICON_WARNING, this);
    return;
  }
  if ((provider.Contains("Copernicus Marine North-West Shelf") || provider == "Auto") &&
      (m_username->GetValue().empty() || m_password->GetValue().empty())) {
    wxString message = "Enter your Copernicus Marine username and password for this operation. The password is held in memory only and is not passed on the command line.";
    AppendLog(message);
    wxMessageBox(message, "Missing Copernicus credentials", wxOK | wxICON_WARNING, this);
    return;
  }
  if ((provider.Contains("Copernicus Marine North-West Shelf") || provider == "Auto") &&
      !ConfirmLargeCopernicusRequest()) {
    AppendLog("Generation cancelled before launch.");
    return;
  }
  wxString command = BuildGenerateCommand();
  wxFileName output(OutputPath());
  if (!output.DirExists()) {
    output.Mkdir(wxS_DIR_DEFAULT, wxPATH_MKDIR_FULL);
  }
  if (provider.Contains("Copernicus Marine North-West Shelf") || provider == "Auto") {
    wxFileName downloadDir;
    downloadDir.AssignDir(m_outputDir->GetPath());
    downloadDir.AppendDir("currentgrib_downloads");
    if (!downloadDir.DirExists()) {
      downloadDir.Mkdir(wxS_DIR_DEFAULT, wxPATH_MKDIR_FULL);
    }
  }
  AppendLog("Starting generation...");
  StartCommand(command, m_password->GetValue(), true);
}

void CurrentGribDialog::OnBrowseOutput(wxCommandEvent&) {
  wxFileDialog dialog(this, "Choose output GRIB path", m_outputDir->GetPath(), m_outputFile->GetValue(),
                      "GRIB files (*.grb;*.grib)|*.grb;*.grib|All files (*.*)|*.*",
                      wxFD_SAVE | wxFD_OVERWRITE_PROMPT);
  if (dialog.ShowModal() != wxID_OK) return;
  wxFileName selected(dialog.GetPath());
  m_outputDir->SetPath(selected.GetPath());
  m_outputFile->SetValue(selected.GetFullName());
}

void CurrentGribDialog::OnPresetChanged(wxCommandEvent& event) {
  ApplyPreset(event.GetSelection());
}

void CurrentGribDialog::ApplyPreset(int selection) {
  if (selection == 0) {
    AppendLog("Using custom bbox.");
    return;
  }
  if (selection == 1) {
    if (!m_hasCurrentViewPort) {
      AppendLog("Current chart area is not available yet; enter bbox manually.");
      wxMessageBox("OpenCPN has not provided a valid chart viewport yet. Pan or zoom the chart, then try again.",
                   "Current chart area unavailable", wxOK | wxICON_INFORMATION, this);
      m_presetChoice->SetSelection(0);
      return;
    }
    if (m_currentViewPort.lon_min >= m_currentViewPort.lon_max ||
        m_currentViewPort.lat_min >= m_currentViewPort.lat_max) {
      AppendLog("Current chart area crosses an unsupported longitude boundary; enter bbox manually.");
      wxMessageBox("The current chart area cannot be converted to a simple west/south/east/north bbox. Enter bbox manually.",
                   "Current chart area unavailable", wxOK | wxICON_INFORMATION, this);
      m_presetChoice->SetSelection(0);
      return;
    }
    m_west->SetValue(wxString::Format("%.6f", m_currentViewPort.lon_min));
    m_south->SetValue(wxString::Format("%.6f", m_currentViewPort.lat_min));
    m_east->SetValue(wxString::Format("%.6f", m_currentViewPort.lon_max));
    m_north->SetValue(wxString::Format("%.6f", m_currentViewPort.lat_max));
    AppendLog("Applied current chart area preset.");
    m_presetChoice->SetSelection(0);
    return;
  }
  if (selection == 2) {
    m_west->SetValue("-8.5");
    m_south->SetValue("50.5");
    m_east->SetValue("-2.5");
    m_north->SetValue("56.5");
    m_startUtc->SetValue("2026-07-01T00:00:00Z");
    m_durationHours->SetValue(72);
    m_stepHours->SetValue(1);
    m_provider->SetSelection(1);
    m_outputFile->SetValue("plugin_copernicus_live_current_test.grb");
    AppendLog("Applied Irish Sea / North Channel example preset.");
    return;
  }
  if (selection == 3) {
    m_west->SetValue("-5.5");
    m_south->SetValue("53.0");
    m_east->SetValue("-5.0");
    m_north->SetValue("53.5");
    m_startUtc->SetValue("2026-07-01T00:00:00Z");
    m_durationHours->SetValue(3);
    m_stepHours->SetValue(1);
    m_provider->SetSelection(1);
    m_outputFile->SetValue("plugin_copernicus_live_tiny_test.grb");
    AppendLog("Applied Tiny Copernicus connection test preset.");
  }
}

bool CurrentGribDialog::ConfirmLargeCopernicusRequest() {
  double west = 0.0;
  double south = 0.0;
  double east = 0.0;
  double north = 0.0;
  bool parsed = m_west->GetValue().ToDouble(&west) && m_south->GetValue().ToDouble(&south) &&
                m_east->GetValue().ToDouble(&east) && m_north->GetValue().ToDouble(&north);
  double area = parsed ? (east - west) * (north - south) : 0.0;
  if (m_durationHours->GetValue() <= 72 && area <= 12.0) {
    return true;
  }
  wxString message =
      "This Copernicus request is larger than the normal v1 default.\n\n"
      "Duration: " + wxString::Format("%d hours", m_durationHours->GetValue()) +
      "\nApproximate bbox area: " + wxString::Format("%.2f square degrees", area) +
      "\n\nContinue?";
  return wxMessageBox(message, "Confirm Copernicus download", wxYES_NO | wxICON_WARNING, this) == wxYES;
}

void CurrentGribDialog::OnCancel(wxCommandEvent&) {
  if (!m_processRunning || m_processPid == 0) {
    AppendLog("No running process to cancel.");
    return;
  }
  m_processCancelled = true;
  AppendLog(wxString::Format("Cancelling process, pid=%ld", m_processPid));
  wxKillError error = wxKILL_OK;
  wxKill(m_processPid, wxSIGTERM, &error, wxKILL_CHILDREN);
  if (error != wxKILL_OK) {
    AppendLog(wxString::Format("Process cancel request returned wxKillError=%d", static_cast<int>(error)));
  }
}

void CurrentGribDialog::OnClose(wxCommandEvent& event) {
  if (m_processRunning) {
    int response = wxMessageBox(
        "A generation or dependency check is still running. Cancel it?",
        "Operation running",
        wxYES_NO | wxICON_QUESTION,
        this);
    if (response == wxYES) {
      OnCancel(event);
    }
    return;
  }
  (void)event;
  Hide();
}

void CurrentGribDialog::OnDialogClose(wxCloseEvent& event) {
  if (m_processRunning) {
    int response = wxMessageBox(
        "A generation or dependency check is still running. Cancel it?",
        "Operation running",
        wxYES_NO | wxICON_QUESTION,
        this);
    if (response == wxYES) {
      wxCommandEvent dummy;
      OnCancel(dummy);
    }
    event.Veto();
    return;
  }
  Hide();
}

void CurrentGribDialog::OnProcessTimer(wxTimerEvent& event) {
  (void)event;
  DrainProcessOutput();
}

void CurrentGribDialog::OnProcessTerminated(wxProcessEvent& event) {
  AppendLog(wxString::Format("Process completed, pid=%d", static_cast<int>(event.GetPid())));
  DrainProcessOutput();
  FlushProcessOutput();
  FinishCommand(event.GetExitCode(), true);
}

void CurrentGribDialog::AppendLog(const wxString& message) { m_log->AppendText(message + "\n"); }

void CurrentGribDialog::DrainStream(wxInputStream* stream, wxString* buffer, const wxString& prefix) {
  if (!stream || !buffer) return;
  while (stream->CanRead()) {
    char ch = static_cast<char>(stream->GetC());
    if (stream->LastRead() == 0) break;
    if (ch == '\r') continue;
    if (ch == '\n') {
      AppendLog(Redact(prefix + *buffer));
      buffer->clear();
    } else {
      *buffer += wxString::FromUTF8(&ch, 1);
    }
  }
}

void CurrentGribDialog::DrainProcessOutput() {
  if (!m_process) return;
  DrainStream(m_process->GetInputStream(), &m_stdoutBuffer, "");
  DrainStream(m_process->GetErrorStream(), &m_stderrBuffer, "stderr: ");
}

void CurrentGribDialog::FlushProcessOutput() {
  if (!m_stdoutBuffer.empty()) {
    AppendLog(Redact(m_stdoutBuffer));
    m_stdoutBuffer.clear();
  }
  if (!m_stderrBuffer.empty()) {
    AppendLog(Redact("stderr: " + m_stderrBuffer));
    m_stderrBuffer.clear();
  }
}

void CurrentGribDialog::StartCommand(const wxString& command, const wxString& password, bool generation) {
  AppendLog("StartCommand begins");
  if (m_processRunning) {
    AppendLog("Another operation is already running.");
    return;
  }
  m_currentCommand = command;
  m_processGeneration = generation;
  m_processCancelled = false;
  m_processPid = 0;
  m_stdoutBuffer.clear();
  m_stderrBuffer.clear();
  SetBusy(true);
  AppendLog("Command: " + Redact(command));
  if (!password.empty()) {
    AppendLog("Copernicus password will be passed to the helper process through CURRENTGRIB_COPERNICUS_PASSWORD, not on the command line.");
  }

  auto* process = new wxProcess(this);
  process->Redirect();

  wxExecuteEnv env;
  if (!password.empty()) {
    env.env["CURRENTGRIB_COPERNICUS_PASSWORD"] = password;
  }

  long pid = wxExecute(command, wxEXEC_ASYNC | wxEXEC_NODISABLE, process, password.empty() ? nullptr : &env);
  if (pid == 0) {
    AppendLog("Process failed to launch");
    delete process;
    FinishCommand(-1, false);
    return;
  }

  m_process = process;
  m_processRunning = true;
  m_processPid = pid;
  AppendLog(wxString::Format("Process launched, pid=%ld", pid));
  m_processTimer.Start(100);
}

void CurrentGribDialog::FinishCommand(long exit_code, bool launched) {
  m_processTimer.Stop();
  if (m_process) {
    delete m_process;
    m_process = nullptr;
  }
  AppendLog(wxString::Format("Exit status: %ld", exit_code));
  bool generation = m_processGeneration;
  bool cancelled = m_processCancelled;
  wxString command = m_currentCommand;
  m_processGeneration = false;
  m_processCancelled = false;
  m_currentCommand.clear();
  m_processRunning = false;
  m_processPid = 0;
  SetBusy(false);

  if (!launched) {
    wxMessageBox("The generator process failed to launch. Check the generator executable path.",
                 "Launch failed", wxOK | wxICON_ERROR, this);
    return;
  }
  if (cancelled) {
    AppendLog("Process cancelled.");
    return;
  }
  if (exit_code == 0 && generation) {
    wxString message = "Generated current GRIB:\n" + OutputPath();
    if (m_openAfter->GetValue()) {
      TryOpenGeneratedGrib();
      message += "\n\nA request was sent to the GRIB plugin to open this file. If it does not appear, open this GRIB in the GRIB plugin, or merge it with a weather GRIB using GRIB -> Merge GRIBs.";
    } else if (m_showMergeInstructions->GetValue()) {
      message += "\n\nOpen this GRIB in the GRIB plugin, or merge it with a weather GRIB using GRIB -> Merge GRIBs.";
    }
    AppendLog(message);
    wxMessageBox(message, "Current GRIB generated", wxOK | wxICON_INFORMATION, this);
  } else if (exit_code != 0 && generation) {
    if (command.Contains("--use-source-grid")) {
      AppendLog("If this failed while using the NetCDF source grid, retry from the CLI without --use-source-grid to interpolate to a regular grid.");
    }
    wxMessageBox("Current GRIB generation failed. See the log/details area for command output.",
                 "Generation failed", wxOK | wxICON_ERROR, this);
  }
}

void CurrentGribDialog::SetBusy(bool busy) {
  m_checkButton->Enable(!busy);
  m_generateButton->Enable(!busy);
  m_cancelButton->Enable(busy);
  m_closeButton->Enable(true);
}

void CurrentGribDialog::TryOpenGeneratedGrib() {
  wxString path = OutputPath();
  if (!wxFileName::FileExists(path)) {
    AppendLog("Generated GRIB does not exist; not sending GRIB open request.");
    return;
  }
  wxString body = "{\"grib_file\":\"" + JsonEscape(path) + "\"}";
  SendPluginMessage("GRIB_APPLY_JSON_CONFIG", body);
  AppendLog("Sent GRIB plugin open request for: " + path);
}

wxString CurrentGribDialog::BuildGenerateCommand() const {
  wxString provider = m_provider->GetStringSelection();
  if (provider.Contains("Copernicus Marine North-West Shelf") || provider == "Auto") {
    wxFileName downloadDir;
    downloadDir.AssignDir(m_outputDir->GetPath());
    downloadDir.AppendDir("currentgrib_downloads");
    return ShellQuote(m_generatorPath->GetValue()) + " generate-copernicus --bbox " +
           ShellQuote(m_west->GetValue()) + " " + ShellQuote(m_south->GetValue()) + " " +
           ShellQuote(m_east->GetValue()) + " " + ShellQuote(m_north->GetValue()) +
           " --start " + ShellQuote(m_startUtc->GetValue()) +
           " --hours " + wxString::Format("%d", m_durationHours->GetValue()) +
           " --step-hours " + wxString::Format("%d", m_stepHours->GetValue()) +
           " --download-directory " + ShellQuote(downloadDir.GetPath()) +
           " --output " + ShellQuote(OutputPath()) +
           " --username " + ShellQuote(m_username->GetValue()) +
           " --overwrite --metadata-summary --verbose";
  }
  wxString source = "synthetic";
  wxString extra;
  if (provider.Contains("Local NetCDF")) {
    source = "netcdf";
    extra = " --input-netcdf " + ShellQuote(m_localNetcdf->GetPath()) +
            " --clip-bbox-to-source --use-source-grid";
  }
  return ShellQuote(m_generatorPath->GetValue()) + " generate --bbox " + ShellQuote(m_west->GetValue()) + " " +
         ShellQuote(m_south->GetValue()) + " " + ShellQuote(m_east->GetValue()) + " " + ShellQuote(m_north->GetValue()) +
         " --start " + ShellQuote(m_startUtc->GetValue()) +
         " --hours " + wxString::Format("%d", m_durationHours->GetValue()) +
         " --step-hours " + wxString::Format("%d", m_stepHours->GetValue()) +
         " --grid-spacing-deg 0.03 --source " + source + extra +
         " --output " + ShellQuote(OutputPath()) + " --metadata-summary --verbose";
}

wxString CurrentGribDialog::OutputPath() const {
  wxFileName output(m_outputDir->GetPath(), m_outputFile->GetValue());
  return output.GetFullPath();
}

wxString CurrentGribDialog::FindDefaultGenerator() const {
  wxString path;
  if (wxGetEnv("TIDAL_CURRENT_GRIB", &path) && IsExecutableFile(path)) return path;
  if (wxFindFileInPath(&path, wxGetenv("PATH"), "tidal-current-grib")) return path;
  wxString home = wxGetHomeDir();
  wxString dev = home + "/src/tidal-current-grib-generator/.venv/bin/tidal-current-grib";
  if (IsExecutableFile(dev)) return dev;
  return "tidal-current-grib";
}

wxString CurrentGribDialog::Redact(const wxString& text) const {
  wxString redacted(text);
  if (!m_password->GetValue().empty()) redacted.Replace(m_password->GetValue(), "<redacted>");
  if (!m_username->GetValue().empty()) redacted.Replace(m_username->GetValue(), "<redacted-user>");
  RedactQueryParameter(&redacted, "x-cop-user");
  RedactQueryParameter(&redacted, "username");
  RedactQueryParameter(&redacted, "user");
  RedactQueryParameter(&redacted, "email");
  RedactQueryParameter(&redacted, "token");
  RedactQueryParameter(&redacted, "access_token");
  RedactQueryParameter(&redacted, "password");
  return redacted;
}
