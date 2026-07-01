#include "currentgrib_dialog.h"

#include <wx/datetime.h>
#include <wx/filename.h>
#include <wx/process.h>
#include <wx/stdpaths.h>
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

bool IsExecutableFile(const wxString& path) {
  return wxFileName::FileExists(path);
}

}  // namespace

CurrentGribDialog::CurrentGribDialog(wxWindow* parent)
    : wxDialog(parent, wxID_ANY, "Ocean Current GRIB Generator", wxDefaultPosition,
               wxSize(760, 720), wxDEFAULT_DIALOG_STYLE | wxRESIZE_BORDER) {
  auto* top = new wxBoxSizer(wxVERTICAL);
  auto* grid = new wxFlexGridSizer(2, 8, 8);
  grid->AddGrowableCol(1, 1);

  m_generatorPath = new wxTextCtrl(this, wxID_ANY, FindDefaultGenerator());
  m_west = new wxTextCtrl(this, wxID_ANY, "-8.5");
  m_south = new wxTextCtrl(this, wxID_ANY, "50.5");
  m_east = new wxTextCtrl(this, wxID_ANY, "-2.5");
  m_north = new wxTextCtrl(this, wxID_ANY, "56.5");
  m_startUtc = new wxTextCtrl(this, wxID_ANY, wxDateTime::Now().ToUTC().FormatISOCombined('T') + "Z");
  m_durationHours = new wxSpinCtrl(this, wxID_ANY);
  m_durationHours->SetRange(1, 240);
  m_durationHours->SetValue(72);
  m_stepHours = new wxSpinCtrl(this, wxID_ANY);
  m_stepHours->SetRange(1, 24);
  m_stepHours->SetValue(1);

  wxString providers[] = {"Auto", "Copernicus Marine North-West Shelf high-resolution currents",
                          "Copernicus Marine Global currents", "Local NetCDF file",
                          "Synthetic test source"};
  m_provider = new wxChoice(this, wxID_ANY, wxDefaultPosition, wxDefaultSize, WXSIZEOF(providers), providers);
  m_provider->SetSelection(1);
  m_username = new wxTextCtrl(this, wxID_ANY);
  m_password = new wxTextCtrl(this, wxID_ANY, "", wxDefaultPosition, wxDefaultSize, wxTE_PASSWORD);
  m_rememberUsername = new wxCheckBox(this, wxID_ANY, "Remember username");
  m_localNetcdf = new wxFilePickerCtrl(this, wxID_ANY, "", "Select NetCDF file", "*.nc;*.nc4");
  m_outputDir = new wxDirPickerCtrl(this, wxID_ANY, DefaultOutputDirectory());
  m_outputFile = new wxTextCtrl(this, wxID_ANY, DefaultOutputFilename());
  m_openAfter = new wxCheckBox(this, wxID_ANY, "Open generated current GRIB after creation");
  m_showMergeInstructions = new wxCheckBox(this, wxID_ANY, "Show instructions for merging with weather GRIB");
  m_showMergeInstructions->SetValue(true);

  auto addRow = [&](const wxString& label, wxWindow* control) {
    grid->Add(new wxStaticText(this, wxID_ANY, label), 0, wxALIGN_CENTER_VERTICAL);
    grid->Add(control, 1, wxEXPAND);
  };
  addRow("Generator executable", m_generatorPath);
  addRow("West longitude", m_west);
  addRow("South latitude", m_south);
  addRow("East longitude", m_east);
  addRow("North latitude", m_north);
  addRow("Start UTC", m_startUtc);
  addRow("Duration hours", m_durationHours);
  addRow("Step hours", m_stepHours);
  addRow("Data source", m_provider);
  addRow("Copernicus username", m_username);
  addRow("Copernicus password", m_password);
  addRow("Local NetCDF", m_localNetcdf);
  addRow("Output directory", m_outputDir);
  addRow("Output filename", m_outputFile);

  top->Add(grid, 0, wxEXPAND | wxALL, 12);
  top->Add(m_rememberUsername, 0, wxLEFT | wxRIGHT | wxBOTTOM, 12);
  top->Add(m_openAfter, 0, wxLEFT | wxRIGHT | wxBOTTOM, 12);
  top->Add(m_showMergeInstructions, 0, wxLEFT | wxRIGHT | wxBOTTOM, 12);

  auto* buttons = new wxBoxSizer(wxHORIZONTAL);
  auto* check = new wxButton(this, wxID_ANY, "Check dependencies");
  auto* generate = new wxButton(this, wxID_OK, "Generate");
  auto* close = new wxButton(this, wxID_CANCEL, "Close");
  buttons->Add(check, 0, wxRIGHT, 8);
  buttons->AddStretchSpacer();
  buttons->Add(generate, 0, wxRIGHT, 8);
  buttons->Add(close);
  top->Add(buttons, 0, wxEXPAND | wxLEFT | wxRIGHT | wxBOTTOM, 12);

  m_log = new wxTextCtrl(this, wxID_ANY, "", wxDefaultPosition, wxDefaultSize,
                         wxTE_MULTILINE | wxTE_READONLY);
  top->Add(m_log, 1, wxEXPAND | wxLEFT | wxRIGHT | wxBOTTOM, 12);
  SetSizer(top);

  check->Bind(wxEVT_BUTTON, &CurrentGribDialog::OnCheckDependencies, this);
  generate->Bind(wxEVT_BUTTON, &CurrentGribDialog::OnGenerate, this);
  close->Bind(wxEVT_BUTTON, &CurrentGribDialog::OnClose, this);

  AppendLog("Generated current GRIBs are model data for planning and experimentation, not official navigation products.");
}

void CurrentGribDialog::OnCheckDependencies(wxCommandEvent&) {
  wxString command = ShellQuote(m_generatorPath->GetValue()) + " check-dependencies --output-directory " +
                     ShellQuote(m_outputDir->GetPath()) + " --json";
  AppendLog("Running dependency check...");
  RunCommandAndLog(command);
}

void CurrentGribDialog::OnGenerate(wxCommandEvent&) {
  wxString provider = m_provider->GetStringSelection();
  if (provider.Contains("Copernicus")) {
    AppendLog("Copernicus download is intentionally stubbed in this plugin scaffold.");
    AppendLog("Use tidal-current-grib download-copernicus from a trusted shell, or select Local NetCDF after downloading.");
    AppendLog("No password was passed to a command line or logged.");
    return;
  }
  wxString command = BuildGenerateCommand();
  AppendLog("Starting generation...");
  RunCommandAndLog(command);
}

void CurrentGribDialog::OnClose(wxCommandEvent&) { Hide(); }

void CurrentGribDialog::AppendLog(const wxString& message) { m_log->AppendText(message + "\n"); }

void CurrentGribDialog::RunCommandAndLog(const wxString& command) {
  AppendLog("Command: " + Redact(command));
  wxArrayString output;
  wxArrayString errors;
  long rc = wxExecute(command, output, errors, wxEXEC_SYNC);
  for (const auto& line : output) AppendLog(Redact(line));
  for (const auto& line : errors) AppendLog(Redact("stderr: " + line));
  AppendLog(wxString::Format("Exit status: %ld", rc));
}

wxString CurrentGribDialog::BuildGenerateCommand() const {
  wxFileName output(m_outputDir->GetPath(), m_outputFile->GetValue());
  wxString provider = m_provider->GetStringSelection();
  wxString source = "synthetic";
  wxString extra;
  if (provider.Contains("Local NetCDF")) {
    source = "netcdf";
    extra = " --input-netcdf " + ShellQuote(m_localNetcdf->GetPath()) +
            " --clip-bbox-to-source --use-source-grid";
  } else if (provider.Contains("Copernicus")) {
    source = "netcdf";
    extra = " --input-netcdf " + ShellQuote("downloaded_copernicus_current.nc") +
            " --clip-bbox-to-source --use-source-grid";
  }
  return ShellQuote(m_generatorPath->GetValue()) + " generate --bbox " + ShellQuote(m_west->GetValue()) + " " +
         ShellQuote(m_south->GetValue()) + " " + ShellQuote(m_east->GetValue()) + " " + ShellQuote(m_north->GetValue()) +
         " --start " + ShellQuote(m_startUtc->GetValue()) +
         " --hours " + wxString::Format("%d", m_durationHours->GetValue()) +
         " --step-hours " + wxString::Format("%d", m_stepHours->GetValue()) +
         " --grid-spacing-deg 0.03 --source " + source + extra +
         " --output " + ShellQuote(output.GetFullPath()) + " --metadata-summary";
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
  return redacted;
}
