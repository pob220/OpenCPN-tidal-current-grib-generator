#include "currentgrib_dialog.h"

#include <wx/config.h>
#include <wx/datetime.h>
#include <wx/filedlg.h>
#include <wx/filename.h>
#include <wx/msgdlg.h>
#include <wx/process.h>
#include <wx/scrolwin.h>
#include <wx/stdpaths.h>
#include <wx/stream.h>
#include <wx/utils.h>
#include <wx/file.h>

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <vector>

#ifdef __UNIX__
#include <signal.h>
#endif

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

wxString TimestampedFilename(const wxString& prefix) {
  return prefix + "_" + wxDateTime::Now().ToUTC().Format("%Y%m%d_%H%M") + ".grb";
}

wxString DefaultStartUtc() {
  wxDateTime now = wxDateTime::Now().ToUTC();
  now.SetMinute(0);
  now.SetSecond(0);
  now.SetMillisecond(0);
  return now.FormatISOCombined('T') + "Z";
}

bool IsCopernicusProvider(const wxString& provider) {
  return provider == "Auto" || provider.Contains("Copernicus Marine North-West Shelf") ||
         provider.Contains("Copernicus Marine Global");
}

bool IsMarineIeProvider(const wxString& provider) {
  return provider.Contains("Marine Institute Ireland");
}

wxString CopernicusProviderId(const wxString& provider) {
  if (provider.Contains("Copernicus Marine North-West Shelf")) return "copernicus_nws";
  if (provider.Contains("Copernicus Marine Global")) return "copernicus_global";
  return "auto";
}

wxString RemoteProviderId(const wxString& provider) {
  if (IsMarineIeProvider(provider)) return "marine_ie_irish_sea";
  return CopernicusProviderId(provider);
}

wxString MarineIeOutputFilename() {
  return TimestampedFilename("marine_ie_irish_sea_current");
}

wxString DefaultTpxoOutputFilename() {
  return TimestampedFilename("tpxo10_astronomical_tide_current");
}

wxString IrishSeaTpxoOutputFilename() {
  return TimestampedFilename("tpxo10_irish_sea_astronomical_tide_current");
}

wxString DefaultTpxoModelDirectory() {
  wxFileName path(wxGetHomeDir(), "");
  path.AppendDir("OpenCPN");
  path.AppendDir("tide-models");
  return path.GetPath();
}

wxString DefaultTpxoCacheFile() {
  wxFileName path(DefaultOutputDirectory(), "");
  path.AppendDir("tpxo-cache");
  path.SetFullName(TimestampedFilename("tpxo10_astronomical_tide_current_cache"));
  path.SetExt("tpxocache");
  return path.GetFullPath();
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

bool ParseDouble(const wxTextCtrl* control, double* value) {
  return control && control->GetValue().ToDouble(value);
}

bool GribStreamIsStrictlyValid(const wxString& path, wxString* details) {
  wxFile file(path);
  if (!file.IsOpened()) {
    if (details) *details = "could not open output file";
    return false;
  }
  wxFileOffset size = file.Length();
  if (size <= 0) {
    if (details) *details = "output file is empty";
    return false;
  }
  std::vector<unsigned char> buffer(static_cast<size_t>(size));
  wxFileOffset read = file.Read(buffer.data(), buffer.size());
  if (read != size) {
    if (details) *details = "could not read complete output file";
    return false;
  }
  const unsigned char* bytes = buffer.data();
  size_t offset = 0;
  int messages = 0;
  const size_t total = static_cast<size_t>(size);
  while (offset < total) {
    if (offset + 12 > total || bytes[offset] != 'G' || bytes[offset + 1] != 'R' ||
        bytes[offset + 2] != 'I' || bytes[offset + 3] != 'B') {
      if (details) *details = wxString::Format("GRIB marker not found at byte offset %zu", offset);
      return false;
    }
    int edition = bytes[offset + 7];
    size_t messageLength = 0;
    if (edition == 1) {
      messageLength = (static_cast<size_t>(bytes[offset + 4]) << 16) |
                      (static_cast<size_t>(bytes[offset + 5]) << 8) |
                      static_cast<size_t>(bytes[offset + 6]);
    } else if (edition == 2) {
      if (offset + 16 > total) {
        if (details) *details = "truncated GRIB2 header";
        return false;
      }
      for (int i = 8; i < 16; ++i) {
        messageLength = (messageLength << 8) | static_cast<size_t>(bytes[offset + i]);
      }
    } else {
      if (details) *details = wxString::Format("unsupported GRIB edition %d", edition);
      return false;
    }
    if (messageLength < 12 || offset + messageLength > total) {
      if (details) *details = wxString::Format("truncated GRIB message at byte offset %zu", offset);
      return false;
    }
    size_t end = offset + messageLength - 4;
    if (bytes[end] != '7' || bytes[end + 1] != '7' || bytes[end + 2] != '7' || bytes[end + 3] != '7') {
      if (details) *details = wxString::Format("GRIB terminator not found at byte offset %zu", end);
      return false;
    }
    ++messages;
    offset += messageLength;
  }
  if (messages == 0) {
    if (details) *details = "no GRIB messages found";
    return false;
  }
  if (details) *details = wxString::Format("validated GRIB stream: %d messages", messages);
  return true;
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
    : wxDialog(parent, wxID_ANY, "Environmental GRIB Generator", wxDefaultPosition,
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
  wxString presets[] = {"Custom bbox", "Current chart area", "Irish Sea TPXO test area",
                        "Irish Sea Marine Institute 3 day current GRIB",
                        "Irish Sea / North Channel Copernicus example",
                        "Tiny Copernicus connection test",
                        "Global Copernicus tiny connection test"};
  m_presetChoice = new wxChoice(scrolled, wxID_ANY, wxDefaultPosition, wxDefaultSize, WXSIZEOF(presets), presets);
  m_presetChoice->SetSelection(0);
  m_startUtc = new wxTextCtrl(scrolled, wxID_ANY, DefaultStartUtc());
  m_durationHours = new wxSpinCtrl(scrolled, wxID_ANY);
  m_durationHours->SetRange(1, 240);
  m_durationHours->SetValue(72);
  m_stepHours = new wxSpinCtrl(scrolled, wxID_ANY);
  m_stepHours->SetRange(1, 24);
  m_stepHours->SetValue(1);

  m_generateWeather = new wxCheckBox(scrolled, wxID_ANY, "Generate/include weather");
  m_generateWeather->SetValue(true);
  wxString weatherProviders[] = {"NOAA GFS forecast", "Met Office UKV 2 km forecast", "ECMWF IFS Open Data forecast",
                                 "Existing weather GRIB file", "None"};
  m_weatherProvider = new wxChoice(scrolled, wxID_ANY, wxDefaultPosition, wxDefaultSize,
                                   WXSIZEOF(weatherProviders), weatherProviders);
  m_weatherProvider->SetSelection(0);
  wxString weatherPresets[] = {"Minimal wind", "Routing", "Marine comfort"};
  m_weatherPreset = new wxChoice(scrolled, wxID_ANY, wxDefaultPosition, wxDefaultSize,
                                 WXSIZEOF(weatherPresets), weatherPresets);
  m_weatherPreset->SetSelection(1);
  m_weatherPreset->SetToolTip("Minimal: wind only. Routing: wind, pressure, and air temperature. Marine: routing fields plus gusts, precipitation, cloud cover, and optional waves.");
  m_includeWaves = new wxCheckBox(scrolled, wxID_ANY, "Include NOAA GFS Wave fields");
  m_includeWaves->SetToolTip("Adds significant wave height, primary wave period, and primary wave direction from NOAA GFS Wave. Available only with NOAA GFS weather.");
  m_existingWeatherFile = new wxFilePickerCtrl(scrolled, wxID_ANY, "", "Select weather GRIB", "*.grb;*.grb2");

  m_generateCurrents = new wxCheckBox(scrolled, wxID_ANY, "Generate/include currents");
  m_generateCurrents->SetValue(true);
  wxString currentSources[] = {"None",
                               "Existing current GRIB file",
                               "TPXO cache",
                               "TPXO direct astronomical tide model",
                               "Marine.ie Irish Sea latest run",
                               "Copernicus NWS forecast/model currents",
                               "Copernicus Global forecast/model currents",
                               "Auto forecast/model current provider"};
  m_currentSource = new wxChoice(scrolled, wxID_ANY, wxDefaultPosition, wxDefaultSize,
                                 WXSIZEOF(currentSources), currentSources);
  m_currentSource->SetSelection(2);
  m_existingCurrentFile = new wxFilePickerCtrl(scrolled, wxID_ANY, "", "Select current GRIB", "*.grb;*.grb2");

  wxString modes[] = {"Forecast/model current GRIB",
                      "Tidal stream prediction from local TPXO model",
                      "Local NetCDF file",
                      "Synthetic test source"};
  m_mode = new wxChoice(scrolled, wxID_ANY, wxDefaultPosition, wxDefaultSize, WXSIZEOF(modes), modes);
  m_mode->SetSelection(0);

  wxString providers[] = {"Auto", "Copernicus Marine North-West Shelf high-resolution currents",
                          "Copernicus Marine Global currents",
                          "Marine Institute Ireland Irish Sea currents, 3 day"};
  m_provider = new wxChoice(scrolled, wxID_ANY, wxDefaultPosition, wxDefaultSize, WXSIZEOF(providers), providers);
  m_provider->SetSelection(1);
  m_mode->Hide();
  m_provider->Hide();
  m_mode->SetSize(0, 0);
  m_provider->SetSize(0, 0);
  m_username = new wxTextCtrl(scrolled, wxID_ANY);
  m_password = new wxTextCtrl(scrolled, wxID_ANY, "", wxDefaultPosition, wxDefaultSize, wxTE_PASSWORD);
  m_rememberUsername = new wxCheckBox(scrolled, wxID_ANY, "Remember username");
  m_providerNote = new wxStaticText(scrolled, wxID_ANY, "");
  m_tpxoModelDir = new wxDirPickerCtrl(scrolled, wxID_ANY, DefaultTpxoModelDirectory());
  m_tpxoModelName = new wxTextCtrl(scrolled, wxID_ANY, "TPXO10-atlas-v2-nc");
  m_tpxoGridSpacing = new wxTextCtrl(scrolled, wxID_ANY, "0.05");
  m_checkTpxoButton = new wxButton(scrolled, wxID_ANY, "Check TPXO model");
  m_useTpxoCache = new wxCheckBox(scrolled, wxID_ANY, "Use TPXO cache for this area");
  m_tpxoCacheFile = new wxFilePickerCtrl(scrolled, wxID_ANY, DefaultTpxoCacheFile(),
                                         "Select TPXO cache file", "*.tpxocache;*.npz");
  m_prepareTpxoCacheButton = new wxButton(scrolled, wxID_ANY, "Prepare/update cache");
  m_localNetcdf = new wxFilePickerCtrl(scrolled, wxID_ANY, "", "Select NetCDF file", "*.nc;*.nc4");
  m_outputDir = new wxDirPickerCtrl(scrolled, wxID_ANY, DefaultOutputDirectory());
  m_outputFile = new wxTextCtrl(scrolled, wxID_ANY, "");
  auto* outputBrowse = new wxButton(scrolled, wxID_ANY, "Browse...");
  m_openAfter = new wxCheckBox(scrolled, wxID_ANY, "Open generated GRIB after creation");
  m_showMergeInstructions = new wxCheckBox(scrolled, wxID_ANY, "Show final GRIB summary");
  m_showMergeInstructions->SetValue(false);
  m_showMergeInstructions->Hide();
  m_showMergeInstructions->SetSize(0, 0);

  auto addRow = [&](const wxString& label, wxWindow* control) -> wxStaticText* {
    auto* labelControl = new wxStaticText(scrolled, wxID_ANY, label);
    grid->Add(labelControl, 0, wxALIGN_CENTER_VERTICAL);
    grid->Add(control, 1, wxEXPAND);
    return labelControl;
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
  grid->Add(new wxStaticText(scrolled, wxID_ANY, "Weather"), 0, wxALIGN_CENTER_VERTICAL);
  grid->Add(m_generateWeather, 0);
  addRow("Weather provider", m_weatherProvider);
  m_weatherPresetLabel = addRow("Weather preset", m_weatherPreset);
  m_wavesLabel = new wxStaticText(scrolled, wxID_ANY, "Waves");
  grid->Add(m_wavesLabel, 0, wxALIGN_CENTER_VERTICAL);
  grid->Add(m_includeWaves, 0);
  m_existingWeatherFileLabel = addRow("Existing weather GRIB", m_existingWeatherFile);
  grid->Add(new wxStaticText(scrolled, wxID_ANY, "Currents"), 0, wxALIGN_CENTER_VERTICAL);
  grid->Add(m_generateCurrents, 0);
  addRow("Current source", m_currentSource);
  m_existingCurrentFileLabel = addRow("Existing current GRIB", m_existingCurrentFile);
  m_usernameLabel = addRow("Copernicus username", m_username);
  m_passwordLabel = addRow("Copernicus password", m_password);
  addRow("Provider note", m_providerNote);
  m_tpxoModelDirLabel = addRow("TPXO model directory", m_tpxoModelDir);
  m_tpxoModelNameLabel = addRow("TPXO model name", m_tpxoModelName);
  m_tpxoGridSpacingLabel = addRow("TPXO grid spacing degrees", m_tpxoGridSpacing);
  m_checkTpxoLabel = new wxStaticText(scrolled, wxID_ANY, "TPXO model check");
  grid->Add(m_checkTpxoLabel, 0, wxALIGN_CENTER_VERTICAL);
  grid->Add(m_checkTpxoButton, 0);
  auto* tpxoCacheOptionLabel = new wxStaticText(scrolled, wxID_ANY, "TPXO cache");
  tpxoCacheOptionLabel->Hide();
  m_useTpxoCache->Hide();
  grid->Add(tpxoCacheOptionLabel, 0, wxALIGN_CENTER_VERTICAL);
  grid->Add(m_useTpxoCache, 0);
  m_tpxoCacheFileLabel = addRow("TPXO cache file", m_tpxoCacheFile);
  m_prepareTpxoCacheLabel = new wxStaticText(scrolled, wxID_ANY, "TPXO cache preparation");
  grid->Add(m_prepareTpxoCacheLabel, 0, wxALIGN_CENTER_VERTICAL);
  grid->Add(m_prepareTpxoCacheButton, 0);
  m_localNetcdfLabel = addRow("Local NetCDF", m_localNetcdf);
  addRow("Output directory", m_outputDir);
  auto* outputFileSizer = new wxBoxSizer(wxHORIZONTAL);
  outputFileSizer->Add(m_outputFile, 1, wxEXPAND | wxRIGHT, 8);
  outputFileSizer->Add(outputBrowse, 0);
  grid->Add(new wxStaticText(scrolled, wxID_ANY, "Output filename"), 0, wxALIGN_CENTER_VERTICAL);
  grid->Add(outputFileSizer, 1, wxEXPAND);

  form->Add(grid, 0, wxEXPAND | wxALL, 12);
  form->Add(m_rememberUsername, 0, wxLEFT | wxRIGHT | wxBOTTOM, 12);
  form->Add(m_openAfter, 0, wxLEFT | wxRIGHT | wxBOTTOM, 12);
  scrolled->SetSizer(form);
  top->Add(scrolled, 1, wxEXPAND);

  m_log = new wxTextCtrl(this, wxID_ANY, "", wxDefaultPosition, wxSize(-1, 220),
                         wxTE_MULTILINE | wxTE_READONLY | wxTE_DONTWRAP);
  m_log->SetMinSize(wxSize(760, 180));
  top->Add(m_log, 0, wxEXPAND | wxLEFT | wxRIGHT | wxTOP | wxBOTTOM, 12);

  auto* buttons = new wxBoxSizer(wxHORIZONTAL);
  m_checkButton = new wxButton(this, wxID_ANY, "Check Dependencies");
  m_generateButton = new wxButton(this, wxID_OK, "Generate Complete GRIB");
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
  m_checkTpxoButton->Bind(wxEVT_BUTTON, &CurrentGribDialog::OnCheckTpxoModel, this);
  m_prepareTpxoCacheButton->Bind(wxEVT_BUTTON, &CurrentGribDialog::OnPrepareTpxoCache, this);
  outputBrowse->Bind(wxEVT_BUTTON, &CurrentGribDialog::OnBrowseOutput, this);
  m_outputFile->Bind(wxEVT_TEXT, &CurrentGribDialog::OnOutputFilenameChanged, this);
  m_presetChoice->Bind(wxEVT_CHOICE, &CurrentGribDialog::OnPresetChanged, this);
  m_provider->Bind(wxEVT_CHOICE, &CurrentGribDialog::OnProviderChanged, this);
  m_mode->Bind(wxEVT_CHOICE, &CurrentGribDialog::OnModeChanged, this);
  m_weatherProvider->Bind(wxEVT_CHOICE, &CurrentGribDialog::OnProviderChanged, this);
  m_weatherPreset->Bind(wxEVT_CHOICE, &CurrentGribDialog::OnProviderChanged, this);
  m_currentSource->Bind(wxEVT_CHOICE, &CurrentGribDialog::OnProviderChanged, this);
  m_generateWeather->Bind(wxEVT_CHECKBOX, &CurrentGribDialog::OnProviderChanged, this);
  m_generateCurrents->Bind(wxEVT_CHECKBOX, &CurrentGribDialog::OnProviderChanged, this);
  m_includeWaves->Bind(wxEVT_CHECKBOX, &CurrentGribDialog::OnProviderChanged, this);
  m_cancelButton->Bind(wxEVT_BUTTON, &CurrentGribDialog::OnCancel, this);
  m_closeButton->Bind(wxEVT_BUTTON, &CurrentGribDialog::OnClose, this);
  Bind(wxEVT_CLOSE_WINDOW, &CurrentGribDialog::OnDialogClose, this);
  Bind(wxEVT_TIMER, &CurrentGribDialog::OnProcessTimer, this);
  Bind(wxEVT_END_PROCESS, &CurrentGribDialog::OnProcessTerminated, this);

  AppendLog("Generated environmental GRIBs are model data for planning and experimentation, not official navigation products.");
  LoadSettings();
  RefreshOutputFilenameDefault();
  UpdateProviderUi();
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

void CurrentGribDialog::OnCheckTpxoModel(wxCommandEvent&) {
  wxString command = ShellQuote(m_generatorPath->GetValue()) +
                     " inspect-source --source tpxo --model-dir " +
                     ShellQuote(m_tpxoModelDir->GetPath()) + " --model-name " +
                     ShellQuote(m_tpxoModelName->GetValue());
  AppendLog("Checking TPXO model...");
  AppendLog("Source: TPXO10 astronomical tide model");
  StartCommand(command, "", false);
}

void CurrentGribDialog::OnPrepareTpxoCache(wxCommandEvent&) {
  wxFileName cachePath(m_tpxoCacheFile->GetPath());
  if (cachePath.GetFullPath().empty()) {
    wxString message = "Choose a TPXO cache file path before preparing the cache.";
    AppendLog(message);
    wxMessageBox(message, "Missing TPXO cache file", wxOK | wxICON_WARNING, this);
    return;
  }
  if (!cachePath.DirExists()) {
    cachePath.Mkdir(wxS_DIR_DEFAULT, wxPATH_MKDIR_FULL);
  }
  wxString command = ShellQuote(m_generatorPath->GetValue()) + " prepare-tpxo-cache --bbox " +
                     ShellQuote(m_west->GetValue()) + " " + ShellQuote(m_south->GetValue()) + " " +
                     ShellQuote(m_east->GetValue()) + " " + ShellQuote(m_north->GetValue()) +
                     " --grid-spacing-deg " + ShellQuote(m_tpxoGridSpacing->GetValue()) +
                     " --model-dir " + ShellQuote(m_tpxoModelDir->GetPath()) +
                     " --model-name " + ShellQuote(m_tpxoModelName->GetValue()) +
                     " --output " + ShellQuote(cachePath.GetFullPath()) +
                     " --metadata-summary --verbose";
  AppendLog("Preparing TPXO cache...");
  AppendLog("Source: TPXO10 astronomical tide model");
  AppendLog("TPXO cache files are derived from local licensed TPXO model files. Do not redistribute unless your TPXO licence permits it.");
  SaveSettings();
  StartCommand(command, "", false);
}

void CurrentGribDialog::OnGenerate(wxCommandEvent&) {
  int mode = m_mode->GetSelection();
  wxString provider = m_provider->GetStringSelection();
  wxString weatherProvider = m_weatherProvider->GetStringSelection();
  wxString currentSource = m_currentSource->GetStringSelection();
  if (m_generateWeather->GetValue() && weatherProvider.Contains("Existing") &&
      m_existingWeatherFile->GetPath().empty()) {
    wxString message = "Select an existing weather GRIB file or choose a generated weather provider.";
    AppendLog(message);
    wxMessageBox(message, "Missing weather GRIB", wxOK | wxICON_WARNING, this);
    return;
  }
  if (m_generateCurrents->GetValue() && currentSource.Contains("Existing") &&
      m_existingCurrentFile->GetPath().empty()) {
    wxString message = "Select an existing current GRIB file or choose TPXO cache/None.";
    AppendLog(message);
    wxMessageBox(message, "Missing current GRIB", wxOK | wxICON_WARNING, this);
    return;
  }
  if (m_generateCurrents->GetValue() && currentSource.Contains("TPXO cache")) {
    wxString cachePath = m_tpxoCacheFile->GetPath();
    if (cachePath.empty()) {
      cachePath = DefaultTpxoCacheFile();
      m_tpxoCacheFile->SetPath(cachePath);
    }
    if (m_tpxoModelDir->GetPath().empty() || m_tpxoModelName->GetValue().empty()) {
      wxString message = "Select a TPXO model directory and model name before TPXO cache generation.";
      AppendLog(message);
      wxMessageBox(message, "Missing TPXO model", wxOK | wxICON_WARNING, this);
      return;
    }
    if (!wxFileName::FileExists(cachePath)) {
      wxString message =
          "No suitable TPXO cache exists for this area/grid/model. Prepare/update it now? This may take about a minute.";
      int response = wxMessageBox(message, "Prepare TPXO cache", wxYES_NO | wxICON_QUESTION, this);
      if (response != wxYES) {
        AppendLog("Generation cancelled: TPXO cache preparation was declined.");
        return;
      }
      AppendLog("TPXO cache missing; generation will prepare/update it before merging.");
    }
  }
  if (m_generateCurrents->GetValue() && currentSource.Contains("TPXO direct") &&
      (m_tpxoModelDir->GetPath().empty() || m_tpxoModelName->GetValue().empty())) {
    wxString message = "Select a TPXO model directory and model name before direct TPXO generation.";
    AppendLog(message);
    wxMessageBox(message, "Missing TPXO model", wxOK | wxICON_WARNING, this);
    return;
  }
  bool copernicusForecast = m_generateCurrents->GetValue() &&
      (currentSource.Contains("Copernicus") ||
       (currentSource.Contains("Auto") && !AutoWouldUseMarineIe()));
  if (copernicusForecast && (m_username->GetValue().empty() || m_password->GetValue().empty())) {
    wxString message = "Enter your Copernicus Marine username and password for this operation. The password is held in memory only and is not passed on the command line.";
    AppendLog(message);
    wxMessageBox(message, "Missing Copernicus credentials", wxOK | wxICON_WARNING, this);
    return;
  }
  if (copernicusForecast && !ConfirmLargeCopernicusRequest()) {
    AppendLog("Generation cancelled before launch.");
    return;
  }
  if (m_generateWeather->GetValue() && weatherProvider.Contains("Met Office UKV") && !ValidateUkvRequest()) {
    AppendLog("Generation cancelled before launch.");
    return;
  }
  wxString command = BuildGenerateCommand();
  wxFileName output(OutputPath());
  if (!output.DirExists()) {
    output.Mkdir(wxS_DIR_DEFAULT, wxPATH_MKDIR_FULL);
  }
  if (copernicusForecast) {
    wxFileName downloadDir;
    downloadDir.AssignDir(m_outputDir->GetPath());
    downloadDir.AppendDir("currentgrib_downloads");
    if (!downloadDir.DirExists()) {
      downloadDir.Mkdir(wxS_DIR_DEFAULT, wxPATH_MKDIR_FULL);
    }
  }
  SaveSettings();
  AppendLog("Starting environmental GRIB generation...");
  AppendLog("Source: " + SourceLabel());
  wxString childPassword = copernicusForecast ? m_password->GetValue() : "";
  StartCommand(command, childPassword, true);
}

void CurrentGribDialog::OnBrowseOutput(wxCommandEvent&) {
  wxFileDialog dialog(this, "Choose output GRIB path", m_outputDir->GetPath(), m_outputFile->GetValue(),
                      "GRIB files (*.grb;*.grib)|*.grb;*.grib|All files (*.*)|*.*",
                      wxFD_SAVE | wxFD_OVERWRITE_PROMPT);
  if (dialog.ShowModal() != wxID_OK) return;
  wxFileName selected(dialog.GetPath());
  m_outputDir->SetPath(selected.GetPath());
  m_outputFileUserCustomized = true;
  m_outputFile->SetValue(selected.GetFullName());
}

void CurrentGribDialog::OnOutputFilenameChanged(wxCommandEvent&) {
  if (m_updatingOutputFilename) return;
  wxString value = m_outputFile->GetValue();
  if (value.empty()) {
    m_outputFileUserCustomized = false;
    RefreshOutputFilenameDefault();
    return;
  }
  if (!m_lastAutoOutputFilename.empty() && value == m_lastAutoOutputFilename) {
    m_outputFileUserCustomized = false;
    return;
  }
  m_outputFileUserCustomized = true;
}

void CurrentGribDialog::OnPresetChanged(wxCommandEvent& event) {
  ApplyPreset(event.GetSelection());
}

void CurrentGribDialog::OnProviderChanged(wxCommandEvent&) {
  RefreshOutputFilenameDefault();
  UpdateProviderUi();
}

void CurrentGribDialog::OnModeChanged(wxCommandEvent&) {
  RefreshOutputFilenameDefault();
  UpdateProviderUi();
}

void CurrentGribDialog::ApplyPreset(int selection) {
  if (selection == 0) {
    RefreshOutputFilenameDefault();
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
    RefreshOutputFilenameDefault();
    AppendLog("Applied current chart area preset.");
    m_presetChoice->SetSelection(0);
    return;
  }
  if (selection == 2) {
    m_west->SetValue("-6.0");
    m_south->SetValue("53.0");
    m_east->SetValue("-5.5");
    m_north->SetValue("53.5");
    m_startUtc->SetValue("2026-07-04T00:00:00Z");
    m_durationHours->SetValue(6);
    m_stepHours->SetValue(1);
    m_tpxoGridSpacing->SetValue("0.05");
    m_generateCurrents->SetValue(true);
    m_currentSource->SetSelection(3);
    RefreshOutputFilenameDefault();
    UpdateProviderUi();
    AppendLog("Applied Irish Sea TPXO test area preset.");
    return;
  }
  if (selection == 3) {
    m_west->SetValue("-6.994");
    m_south->SetValue("51.506");
    m_east->SetValue("-4.006");
    m_north->SetValue("55.494");
    m_durationHours->SetValue(72);
    m_stepHours->SetValue(1);
    m_generateCurrents->SetValue(true);
    m_currentSource->SetSelection(4);
    RefreshOutputFilenameDefault();
    UpdateProviderUi();
    AppendLog("Applied Irish Sea Marine Institute 3 day current GRIB preset.");
    return;
  }
  if (selection == 4) {
    m_west->SetValue("-8.5");
    m_south->SetValue("50.5");
    m_east->SetValue("-2.5");
    m_north->SetValue("56.5");
    m_startUtc->SetValue("2026-07-01T00:00:00Z");
    m_durationHours->SetValue(72);
    m_stepHours->SetValue(1);
    m_generateCurrents->SetValue(true);
    m_currentSource->SetSelection(5);
    RefreshOutputFilenameDefault();
    UpdateProviderUi();
    AppendLog("Applied Irish Sea / North Channel example preset.");
    return;
  }
  if (selection == 5) {
    m_west->SetValue("-5.5");
    m_south->SetValue("53.0");
    m_east->SetValue("-5.0");
    m_north->SetValue("53.5");
    m_startUtc->SetValue("2026-07-01T00:00:00Z");
    m_durationHours->SetValue(3);
    m_stepHours->SetValue(1);
    m_generateCurrents->SetValue(true);
    m_currentSource->SetSelection(5);
    RefreshOutputFilenameDefault();
    UpdateProviderUi();
    AppendLog("Applied Tiny Copernicus connection test preset.");
    return;
  }
  if (selection == 6) {
    m_west->SetValue("-40.5");
    m_south->SetValue("30.0");
    m_east->SetValue("-40.0");
    m_north->SetValue("30.5");
    m_startUtc->SetValue("2026-07-01T00:00:00Z");
    m_durationHours->SetValue(6);
    m_stepHours->SetValue(1);
    m_generateCurrents->SetValue(true);
    m_currentSource->SetSelection(6);
    RefreshOutputFilenameDefault();
    UpdateProviderUi();
    AppendLog("Applied Global Copernicus tiny connection test preset.");
  }
}

bool CurrentGribDialog::AutoWouldUseMarineIe() const {
  double west = 0.0;
  double south = 0.0;
  double east = 0.0;
  double north = 0.0;
  bool parsed = m_west->GetValue().ToDouble(&west) && m_south->GetValue().ToDouble(&south) &&
                m_east->GetValue().ToDouble(&east) && m_north->GetValue().ToDouble(&north);
  return parsed && west >= -6.994 && east <= -4.006 && south >= 51.506 && north <= 55.494 &&
         m_durationHours->GetValue() <= 72;
}

void CurrentGribDialog::UpdateProviderUi() {
  int mode = m_mode->GetSelection();
  wxString provider = m_provider->GetStringSelection();
  bool forecastMode = mode == 0;
  bool tpxoMode = mode == 1;
  bool netcdfMode = mode == 2;
  bool syntheticMode = mode == 3;
  bool weatherEnabled = m_generateWeather->GetValue() && m_weatherProvider->GetStringSelection() != "None";
  bool currentsEnabled = m_generateCurrents->GetValue() && m_currentSource->GetStringSelection() != "None";
  bool weatherExisting = weatherEnabled && m_weatherProvider->GetStringSelection().Contains("Existing");
  bool currentExisting = currentsEnabled && m_currentSource->GetStringSelection().Contains("Existing");
  bool currentTpxoCache = currentsEnabled && m_currentSource->GetStringSelection().Contains("TPXO cache");
  bool currentTpxoDirect = currentsEnabled && m_currentSource->GetStringSelection().Contains("TPXO direct");
  bool currentMarine = currentsEnabled && m_currentSource->GetStringSelection().Contains("Marine.ie");
  bool currentCopernicus = currentsEnabled &&
      (m_currentSource->GetStringSelection().Contains("Copernicus") ||
       (m_currentSource->GetStringSelection().Contains("Auto") && !AutoWouldUseMarineIe()));
  bool weatherGfs = weatherEnabled && m_weatherProvider->GetStringSelection().Contains("GFS");
  bool weatherUkv = weatherEnabled && m_weatherProvider->GetStringSelection().Contains("Met Office UKV");
  bool weatherGenerated = weatherEnabled && !weatherExisting;
  bool showTpxoModel = currentTpxoDirect || currentTpxoCache;

  auto showPair = [](wxWindow* label, wxWindow* control, bool show) {
    if (label) label->Show(show);
    if (control) control->Show(show);
  };

  m_provider->Enable(false);
  m_weatherProvider->Enable(m_generateWeather->GetValue());
  showPair(m_weatherPresetLabel, m_weatherPreset, weatherGenerated);
  showPair(m_wavesLabel, m_includeWaves, weatherGfs);
  showPair(m_existingWeatherFileLabel, m_existingWeatherFile, weatherExisting);
  m_weatherPreset->Enable(weatherGenerated);
  m_includeWaves->Enable(weatherGfs);
  if (!weatherGfs) {
    m_includeWaves->SetValue(false);
  }
  m_existingWeatherFile->Enable(weatherExisting);
  m_currentSource->Enable(m_generateCurrents->GetValue());
  showPair(m_existingCurrentFileLabel, m_existingCurrentFile, currentExisting);
  showPair(m_usernameLabel, m_username, currentCopernicus);
  showPair(m_passwordLabel, m_password, currentCopernicus);
  showPair(m_tpxoModelDirLabel, m_tpxoModelDir, showTpxoModel);
  showPair(m_tpxoModelNameLabel, m_tpxoModelName, showTpxoModel);
  showPair(m_tpxoGridSpacingLabel, m_tpxoGridSpacing, showTpxoModel);
  showPair(m_checkTpxoLabel, m_checkTpxoButton, showTpxoModel);
  showPair(m_tpxoCacheFileLabel, m_tpxoCacheFile, currentTpxoCache);
  showPair(m_prepareTpxoCacheLabel, m_prepareTpxoCacheButton, currentTpxoCache);
  showPair(m_localNetcdfLabel, m_localNetcdf, false);
  m_rememberUsername->Show(currentCopernicus);
  m_existingCurrentFile->Enable(currentExisting);
  m_username->Enable(currentCopernicus);
  m_password->Enable(currentCopernicus);
  m_rememberUsername->Enable(currentCopernicus);
  m_tpxoModelDir->Enable(showTpxoModel);
  m_tpxoModelName->Enable(showTpxoModel);
  m_tpxoGridSpacing->Enable(showTpxoModel);
  m_checkTpxoButton->Enable(showTpxoModel && !m_processRunning);
  m_useTpxoCache->Enable(false);
  m_tpxoCacheFile->Enable(currentTpxoCache);
  m_prepareTpxoCacheButton->Enable(currentTpxoCache && !m_processRunning);
  m_localNetcdf->Enable(false);

  if (weatherUkv) {
    wxString note =
        "Source: Met Office UKV 2 km forecast. Met Office UKV 2 km is a high-resolution UK/Ireland short-range forecast. "
        "The plugin converts the source NetCDF data to OpenCPN GRIB in the background.\n"
        "UKV weather is hourly to about 54h and 3-hourly thereafter. Currents may remain hourly. "
        "Requests outside the UK/Ireland domain or beyond available hours will fail clearly.";
    if (m_weatherPreset->GetStringSelection().Contains("Marine")) {
      note += "\nUKV marine extras are not implemented yet; routing fields will be generated.";
    }
    m_providerNote->SetLabel(note);
  } else if (weatherEnabled && m_weatherProvider->GetStringSelection().Contains("ECMWF")) {
    m_providerNote->SetLabel("Source: ECMWF IFS Open Data forecast. Warning: this provider is not spatially cropped yet, so files may be large.");
  } else if (weatherGfs) {
    wxString note = "Source: NOAA GFS forecast via NOMADS. Bbox-subset weather is compact; optional GFS Wave adds significant wave height, primary wave period, and primary wave direction.";
    if (m_includeWaves->GetValue() && m_stepHours->GetValue() != 3) {
      note += wxString::Format("\nWave fields are included every 3 hours; wind/weather and currents remain every %d hour%s.",
                               m_stepHours->GetValue(), m_stepHours->GetValue() == 1 ? "" : "s");
    }
    m_providerNote->SetLabel(note);
  } else if (currentTpxoCache) {
    m_providerNote->SetLabel("Source: TPXO10 astronomical tide model cache. Produces astronomical tidal currents from a local derived cache.");
  } else if (currentTpxoDirect) {
    m_providerNote->SetLabel("Source: TPXO10 astronomical tide model. Astronomical tide only; does not include surge, wind residual current, river flow, or forecast-model corrections.");
  } else if (currentMarine) {
    m_providerNote->SetLabel("Source: Marine.ie Irish Sea latest run. No credentials; valid time range depends on provider run time.");
  } else if (currentCopernicus) {
    m_providerNote->SetLabel("Source: Copernicus Marine forecast/model currents. Username/password are used for this operation only; password is passed via environment, not command line.");
  } else if (!currentsEnabled) {
    m_providerNote->SetLabel("Currents disabled. Output will be weather-only if weather is enabled.");
  } else {
    m_providerNote->SetLabel("");
  }
  if (auto* scrolled = dynamic_cast<wxScrolledWindow*>(m_generatorPath->GetParent())) {
    scrolled->FitInside();
  }
  Layout();
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

bool CurrentGribDialog::ValidateUkvRequest() {
  if (m_stepHours->GetValue() == 1 && m_durationHours->GetValue() > 54) {
    wxString message =
        "Met Office UKV is hourly to about 54h, then 3-hourly to 120h.\n"
        "Continue with mixed-cadence UKV weather?\n"
        "Currents will remain at the selected interval where supported.";
    AppendLog(message);
    if (wxMessageBox(message, "Confirm mixed-cadence UKV weather", wxYES_NO | wxICON_WARNING, this) != wxYES) {
      return false;
    }
  }
  double west = 0.0;
  double south = 0.0;
  double east = 0.0;
  double north = 0.0;
  if (m_west->GetValue().ToDouble(&west) && m_south->GetValue().ToDouble(&south) &&
      m_east->GetValue().ToDouble(&east) && m_north->GetValue().ToDouble(&north)) {
    if (west < -12.0 || east > 4.0 || south < 48.0 || north > 62.0) {
      wxString message =
          "The requested bbox is outside the Met Office UKV UK/Ireland regional domain. Choose a UK/Ireland area or use GFS/ECMWF.";
      AppendLog(message);
      wxMessageBox(message, "UKV area unavailable", wxOK | wxICON_WARNING, this);
      return false;
    }
  }
  if (m_weatherPreset->GetStringSelection().Contains("Marine")) {
    AppendLog("UKV marine extras are not implemented yet; routing fields will be generated.");
  }
  return true;
}

void CurrentGribDialog::OnCancel(wxCommandEvent&) {
  if (!m_processRunning || m_processPid == 0) {
    AppendLog("No running process to cancel.");
    return;
  }
  if (!ChildProcessStillExists()) {
    AppendLog(wxString::Format("Process pid=%ld has already exited; cleaning up dialog state.", m_processPid));
    DrainProcessOutput();
    FlushProcessOutput();
    FinishCommand(-1, true);
    return;
  }
  m_processCancelled = true;
  AppendLog(wxString::Format("Cancelling process, pid=%ld", m_processPid));
  wxKillError error = wxKILL_OK;
  wxKill(m_processPid, wxSIGTERM, &error, wxKILL_CHILDREN);
  if (error != wxKILL_OK) {
    AppendLog(wxString::Format("Process cancel request returned wxKillError=%d", static_cast<int>(error)));
    if (!ChildProcessStillExists()) {
      AppendLog("Process was already gone after cancel request; treating it as exited.");
      DrainProcessOutput();
      FlushProcessOutput();
      FinishCommand(-1, true);
      return;
    }
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
  SaveSettings();
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
  SaveSettings();
  Hide();
}

void CurrentGribDialog::OnProcessTimer(wxTimerEvent& event) {
  (void)event;
  DrainProcessOutput();
  if (m_processRunning && m_processPid != 0 && !ChildProcessStillExists()) {
    AppendLog(wxString::Format("Process pid=%ld is no longer running; finalizing without wxEVT_END_PROCESS.", m_processPid));
    FlushProcessOutput();
    FinishCommand(-1, true);
  }
}

void CurrentGribDialog::OnProcessTerminated(wxProcessEvent& event) {
  if (!m_processRunning || event.GetPid() != m_processPid) {
    AppendLog(wxString::Format("Ignoring stale process completion event, pid=%d", static_cast<int>(event.GetPid())));
    return;
  }
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
  wxString validationDetails;
  bool outputValid = generation && OutputFileLooksValidGrib(&validationDetails);
  if (outputValid && exit_code != 0) {
    AppendLog(validationDetails);
  }
  bool generationSucceeded = generation && (exit_code == 0 || (exit_code < 0 && outputValid));
  if (cancelled && !generationSucceeded) {
    AppendLog("Process cancelled.");
    return;
  }
  if (generationSucceeded) {
    wxString message = "Generated environmental GRIB\nSource: " + SourceLabel() +
                       "\nValid time: " + ValidTimeSummary() +
                       "\nMessages: see validation summary in log" +
                       "\nOutput: " + OutputPath();
    if (m_openAfter->GetValue()) {
      TryOpenGeneratedGrib();
      message += "\n\nA request was sent to the GRIB plugin to open this file. If it does not appear, open this GRIB in the GRIB plugin.";
    } else {
      message += "\n\nOpen this GRIB in the GRIB plugin. It is already a merged environmental GRIB when both weather and currents were selected.";
    }
    AppendLog(message);
    wxMessageBox(message, "Environmental GRIB generated", wxOK | wxICON_INFORMATION, this);
  } else if (exit_code != 0 && generation) {
    if (command.Contains("--use-source-grid")) {
      AppendLog("If this failed while using the NetCDF source grid, retry from the CLI without --use-source-grid to interpolate to a regular grid.");
    }
    wxMessageBox("Environmental GRIB generation failed. See the log/details area for command output.",
                 "Generation failed", wxOK | wxICON_ERROR, this);
  }
}

bool CurrentGribDialog::ChildProcessStillExists() const {
  if (!m_processRunning || m_processPid == 0) return false;
#ifdef __UNIX__
  errno = 0;
  if (kill(static_cast<pid_t>(m_processPid), 0) == 0) return true;
  if (errno == ESRCH) return false;
  return true;
#else
  return true;
#endif
}

bool CurrentGribDialog::OutputFileLooksValidGrib(wxString* details) const {
  wxString path = OutputPath();
  if (!wxFileName::FileExists(path)) {
    if (details) *details = "output file does not exist";
    return false;
  }
  return GribStreamIsStrictlyValid(path, details);
}

void CurrentGribDialog::SetBusy(bool busy) {
  m_checkButton->Enable(!busy);
  m_generateButton->Enable(!busy);
  m_cancelButton->Enable(busy);
  m_closeButton->Enable(true);
  UpdateProviderUi();
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
  wxString weatherProvider = "none";
  if (m_generateWeather->GetValue()) {
    wxString selected = m_weatherProvider->GetStringSelection();
    if (selected.Contains("NOAA GFS")) weatherProvider = "gfs";
    else if (selected.Contains("Met Office UKV")) weatherProvider = "ukmo_ukv";
    else if (selected.Contains("ECMWF")) weatherProvider = "ecmwf_ifs_open";
    else if (selected.Contains("Existing")) weatherProvider = "existing-file";
  }
  wxString weatherPreset = "routing";
  if (m_weatherPreset->GetStringSelection().Contains("Minimal")) weatherPreset = "minimal";
  else if (m_weatherPreset->GetStringSelection().Contains("Marine")) weatherPreset = "marine";

  wxString currentSource = "none";
  if (m_generateCurrents->GetValue()) {
    wxString selectedCurrent = m_currentSource->GetStringSelection();
    if (selectedCurrent.Contains("TPXO cache")) currentSource = "tpxo-cache";
    else if (selectedCurrent.Contains("TPXO direct")) currentSource = "tpxo";
    else if (selectedCurrent.Contains("Existing")) currentSource = "existing-file";
    else if (selectedCurrent.Contains("Marine.ie")) currentSource = "marine_ie_irish_sea";
    else if (selectedCurrent.Contains("NWS")) currentSource = "copernicus_nws";
    else if (selectedCurrent.Contains("Global")) currentSource = "copernicus_global";
    else if (selectedCurrent.Contains("Auto")) currentSource = "auto";
  }

  wxString command = ShellQuote(m_generatorPath->GetValue()) + " generate-environment-grib --bbox " +
                     ShellQuote(m_west->GetValue()) + " " + ShellQuote(m_south->GetValue()) + " " +
                     ShellQuote(m_east->GetValue()) + " " + ShellQuote(m_north->GetValue()) +
                     " --start " + ShellQuote(m_startUtc->GetValue()) +
                     " --cycle auto --hours " + wxString::Format("%d", m_durationHours->GetValue()) +
                     " --step-hours " + wxString::Format("%d", m_stepHours->GetValue()) +
                     " --weather-provider " + weatherProvider +
                     " --weather-preset " + weatherPreset +
                     " --weather-grid-spacing-deg 0.025" +
                     " --current-source " + currentSource +
                     " --output " + ShellQuote(OutputPath()) +
                     " --overwrite --metadata-summary --verbose";
  if (weatherProvider == "existing-file") {
    command += " --weather-file " + ShellQuote(m_existingWeatherFile->GetPath());
  }
  if (m_includeWaves->GetValue() && weatherProvider == "gfs") {
    command += " --include-waves --wave-step-hours 3";
  }
  if (currentSource == "existing-file") {
    command += " --current-file " + ShellQuote(m_existingCurrentFile->GetPath());
  } else if (currentSource == "tpxo-cache") {
    command += " --input-cache " + ShellQuote(m_tpxoCacheFile->GetPath()) +
               " --auto-prepare-tpxo-cache" +
               " --model-dir " + ShellQuote(m_tpxoModelDir->GetPath()) +
               " --model-name " + ShellQuote(m_tpxoModelName->GetValue()) +
               " --grid-spacing-deg " + ShellQuote(m_tpxoGridSpacing->GetValue());
  } else if (currentSource == "tpxo") {
    command += " --model-dir " + ShellQuote(m_tpxoModelDir->GetPath()) +
               " --model-name " + ShellQuote(m_tpxoModelName->GetValue()) +
               " --grid-spacing-deg " + ShellQuote(m_tpxoGridSpacing->GetValue());
  } else if (currentSource == "copernicus_nws" || currentSource == "copernicus_global" || currentSource == "auto") {
    wxFileName downloadDir;
    downloadDir.AssignDir(m_outputDir->GetPath());
    downloadDir.AppendDir("currentgrib_downloads");
    command += " --download-directory " + ShellQuote(downloadDir.GetPath()) +
               " --password-env CURRENTGRIB_COPERNICUS_PASSWORD";
    if (!m_username->GetValue().empty()) {
      command += " --username " + ShellQuote(m_username->GetValue());
    }
  }
  return command;

  int mode = m_mode->GetSelection();
  wxString provider = m_provider->GetStringSelection();
  if (mode == 1) {
    if (m_useTpxoCache->GetValue()) {
      return ShellQuote(m_generatorPath->GetValue()) + " generate --source tpxo-cache --input-cache " +
             ShellQuote(m_tpxoCacheFile->GetPath()) +
             " --start " + ShellQuote(m_startUtc->GetValue()) +
             " --hours " + wxString::Format("%d", m_durationHours->GetValue()) +
             " --step-hours " + wxString::Format("%d", m_stepHours->GetValue()) +
             " --output " + ShellQuote(OutputPath()) + " --metadata-summary --verbose";
    }
    return ShellQuote(m_generatorPath->GetValue()) + " generate --bbox " + ShellQuote(m_west->GetValue()) + " " +
           ShellQuote(m_south->GetValue()) + " " + ShellQuote(m_east->GetValue()) + " " + ShellQuote(m_north->GetValue()) +
           " --start " + ShellQuote(m_startUtc->GetValue()) +
           " --hours " + wxString::Format("%d", m_durationHours->GetValue()) +
           " --step-hours " + wxString::Format("%d", m_stepHours->GetValue()) +
           " --grid-spacing-deg " + ShellQuote(m_tpxoGridSpacing->GetValue()) +
           " --source tpxo --model-dir " + ShellQuote(m_tpxoModelDir->GetPath()) +
           " --model-name " + ShellQuote(m_tpxoModelName->GetValue()) +
           " --output " + ShellQuote(OutputPath()) + " --metadata-summary --verbose";
  }
  if (mode == 0 && (IsCopernicusProvider(provider) || IsMarineIeProvider(provider))) {
    wxFileName downloadDir;
    downloadDir.AssignDir(m_outputDir->GetPath());
    downloadDir.AppendDir("currentgrib_downloads");
    wxString command = ShellQuote(m_generatorPath->GetValue()) + " generate-provider --provider " +
           RemoteProviderId(provider) + " --bbox " +
           ShellQuote(m_west->GetValue()) + " " + ShellQuote(m_south->GetValue()) + " " +
           ShellQuote(m_east->GetValue()) + " " + ShellQuote(m_north->GetValue()) +
           " --start " + ShellQuote(m_startUtc->GetValue()) +
           " --hours " + wxString::Format("%d", m_durationHours->GetValue()) +
           " --step-hours " + wxString::Format("%d", m_stepHours->GetValue()) +
           " --download-directory " + ShellQuote(downloadDir.GetPath()) +
           " --output " + ShellQuote(OutputPath());
    if (!m_username->GetValue().empty()) {
      command += " --username " + ShellQuote(m_username->GetValue());
    }
    command += " --overwrite --metadata-summary --verbose";
    return command;
  }
  wxString source = "synthetic";
  wxString extra;
  wxString spacing = "0.03";
  if (mode == 2) {
    source = "netcdf";
    extra = " --input-netcdf " + ShellQuote(m_localNetcdf->GetPath()) +
            " --clip-bbox-to-source --use-source-grid";
  }
  return ShellQuote(m_generatorPath->GetValue()) + " generate --bbox " + ShellQuote(m_west->GetValue()) + " " +
         ShellQuote(m_south->GetValue()) + " " + ShellQuote(m_east->GetValue()) + " " + ShellQuote(m_north->GetValue()) +
         " --start " + ShellQuote(m_startUtc->GetValue()) +
         " --hours " + wxString::Format("%d", m_durationHours->GetValue()) +
         " --step-hours " + wxString::Format("%d", m_stepHours->GetValue()) +
         " --grid-spacing-deg " + spacing + " --source " + source + extra +
         " --output " + ShellQuote(OutputPath()) + " --metadata-summary --verbose";
}

wxString CurrentGribDialog::OutputPath() const {
  wxFileName output(m_outputDir->GetPath(), m_outputFile->GetValue());
  return output.GetFullPath();
}

wxString CurrentGribDialog::SourceLabel() const {
  wxString weather = m_generateWeather->GetValue() ? m_weatherProvider->GetStringSelection() : "None";
  wxString current = m_generateCurrents->GetValue() ? m_currentSource->GetStringSelection() : "None";
  return "Environmental GRIB: weather=" + weather + ", currents=" + current;

  int mode = m_mode->GetSelection();
  if (mode == 1 && m_useTpxoCache->GetValue()) return "TPXO10 astronomical tide model cache";
  if (mode == 1) return "TPXO10 astronomical tide model";
  if (mode == 2) return "Local NetCDF model current";
  if (mode == 3) return "Synthetic test current";
  wxString provider = m_provider->GetStringSelection();
  if (IsMarineIeProvider(provider) || (provider == "Auto" && AutoWouldUseMarineIe())) {
    return "Marine Institute Ireland Irish Sea forecast/model current";
  }
  if (provider.Contains("North-West Shelf")) {
    return "Copernicus Marine NWS forecast/model current";
  }
  if (provider.Contains("Global")) {
    return "Copernicus Marine Global forecast/model current";
  }
  return "Auto forecast/model current provider";
}

wxString CurrentGribDialog::ValidTimeSummary() const {
  wxString startText = m_startUtc->GetValue();
  wxString parseText = startText;
  if (parseText.EndsWith("Z")) parseText.RemoveLast();
  wxDateTime start;
  if (start.ParseISOCombined(parseText, 'T')) {
    wxDateTime end = start + wxTimeSpan::Hours(m_durationHours->GetValue());
    return start.FormatISOCombined('T') + "Z to " + end.FormatISOCombined('T') + "Z";
  }
  return startText + " plus " + wxString::Format("%d hours", m_durationHours->GetValue());
}

int CurrentGribDialog::ExpectedMessageCount() const {
  int step = std::max(1, m_stepHours->GetValue());
  int timesteps = (m_durationHours->GetValue() / step) + 1;
  return timesteps * 2;
}

wxString CurrentGribDialog::DefaultOutputFilenameForSelection() const {
  wxString prefix;
  bool weatherOn = m_generateWeather->GetValue() && m_weatherProvider->GetStringSelection() != "None";
  bool currentOn = m_generateCurrents->GetValue() && m_currentSource->GetStringSelection() != "None";
  wxString weatherProvider = m_weatherProvider->GetStringSelection();
  wxString currentSource = m_currentSource->GetStringSelection();
  double west = 0.0;
  double south = 0.0;
  double east = 0.0;
  double north = 0.0;
  bool looksIrishSea =
      m_west->GetValue().ToDouble(&west) && m_south->GetValue().ToDouble(&south) &&
      m_east->GetValue().ToDouble(&east) && m_north->GetValue().ToDouble(&north) &&
      std::abs(west - -8.5) < 0.01 && std::abs(south - 50.5) < 0.01 &&
      std::abs(east - -2.5) < 0.01 && std::abs(north - 56.5) < 0.01;
  bool ukvMixedCadence = weatherProvider.Contains("UKV") && m_stepHours->GetValue() == 1 && m_durationHours->GetValue() > 54;
  if (weatherOn && currentOn) {
    prefix = "environment";
    if (weatherProvider.Contains("GFS")) prefix += "_gfs";
    else if (weatherProvider.Contains("UKV")) prefix += "_ukmo_ukv";
    else if (weatherProvider.Contains("ECMWF")) prefix += "_ecmwf";
    else if (weatherProvider.Contains("Existing")) prefix += "_existing_weather";
    if (ukvMixedCadence) prefix += "_mixed";
    if (m_includeWaves->GetValue() && weatherProvider.Contains("GFS")) prefix += "_wave";
    if (currentSource.Contains("TPXO cache")) prefix += "_tpxo_cache";
    else if (currentSource.Contains("TPXO direct")) prefix += "_tpxo";
    else if (currentSource.Contains("Marine.ie")) prefix += "_marine_ie";
    else if (currentSource.Contains("NWS")) prefix += "_copernicus_nws";
    else if (currentSource.Contains("Global")) prefix += "_copernicus_global";
    else if (currentSource.Contains("Auto")) prefix += "_auto_current";
    else if (currentSource.Contains("Existing")) prefix += "_existing_current";
    if (looksIrishSea) prefix += "_irish_sea";
  } else if (weatherOn) {
    prefix = "weather";
    if (weatherProvider.Contains("GFS")) prefix += "_gfs";
    else if (weatherProvider.Contains("UKV")) prefix += "_ukmo_ukv";
    else if (weatherProvider.Contains("ECMWF")) prefix += "_ecmwf";
    else prefix += "_existing";
    if (ukvMixedCadence) prefix += "_mixed";
    if (m_weatherPreset->GetStringSelection().Contains("Marine")) prefix += "_marine";
    if (m_includeWaves->GetValue() && weatherProvider.Contains("GFS")) prefix += "_wave";
    if (looksIrishSea) prefix += "_irish_sea";
  } else if (currentOn) {
    prefix = "current";
    if (currentSource.Contains("TPXO cache")) prefix += "_tpxo_cache";
    else if (currentSource.Contains("TPXO direct")) prefix += "_tpxo";
    else if (currentSource.Contains("Marine.ie")) prefix += "_marine_ie";
    else if (currentSource.Contains("NWS")) prefix += "_copernicus_nws";
    else if (currentSource.Contains("Global")) prefix += "_copernicus_global";
    else if (currentSource.Contains("Auto")) prefix += "_auto";
    else prefix += "_existing";
  }
  if (!prefix.empty()) return TimestampedFilename(prefix);

  int mode = m_mode->GetSelection();
  int preset = m_presetChoice->GetSelection();
  if (mode == 1) {
    return preset == 2 ? IrishSeaTpxoOutputFilename() : DefaultTpxoOutputFilename();
  }
  if (mode == 2) return TimestampedFilename("local_netcdf_current");
  if (mode == 3) return TimestampedFilename("synthetic_current");

  wxString provider = m_provider->GetStringSelection();
  if (IsMarineIeProvider(provider) || (provider == "Auto" && AutoWouldUseMarineIe())) {
    return MarineIeOutputFilename();
  }
  if (provider.Contains("Copernicus Marine Global")) {
    return TimestampedFilename("copernicus_global_current");
  }
  if (provider.Contains("Copernicus Marine North-West Shelf")) {
    return TimestampedFilename("copernicus_nws_current");
  }
  if (provider == "Auto") {
    double west = 0.0;
    double south = 0.0;
    double east = 0.0;
    double north = 0.0;
    bool parsed = m_west->GetValue().ToDouble(&west) && m_south->GetValue().ToDouble(&south) &&
                  m_east->GetValue().ToDouble(&east) && m_north->GetValue().ToDouble(&north);
    if (parsed && west >= -20.0 && east <= 13.0 && south >= 40.0 && north <= 65.0) {
      return TimestampedFilename("copernicus_nws_current");
    }
    return TimestampedFilename("copernicus_global_current");
  }
  return TimestampedFilename("current_grib");
}

void CurrentGribDialog::RefreshOutputFilenameDefault() {
  wxString previousAuto = m_lastAutoOutputFilename;
  wxString current = m_outputFile->GetValue();
  wxString nextAuto = DefaultOutputFilenameForSelection();
  bool shouldUpdate = current.empty() || !m_outputFileUserCustomized ||
                      (!previousAuto.empty() && current == previousAuto);
  m_lastAutoOutputFilename = nextAuto;
  if (!shouldUpdate) return;
  m_updatingOutputFilename = true;
  m_outputFile->SetValue(nextAuto);
  m_updatingOutputFilename = false;
  m_outputFileUserCustomized = false;
}

void CurrentGribDialog::LoadSettings() {
  wxConfigBase* config = wxConfigBase::Get(false);
  if (!config) return;
  wxString oldPath = config->GetPath();
  config->SetPath("/PlugIns/currentgrib_pi");
  long mode = config->ReadLong("generation_mode", m_mode->GetSelection());
  if (mode >= 0 && mode < static_cast<long>(m_mode->GetCount())) {
    m_mode->SetSelection(static_cast<int>(mode));
  }
  wxString value;
  if (config->Read("tpxo_model_directory", &value) && !value.empty()) {
    m_tpxoModelDir->SetPath(value);
  }
  if (config->Read("tpxo_model_name", &value) && !value.empty()) {
    m_tpxoModelName->SetValue(value);
  }
  if (config->Read("tpxo_grid_spacing", &value) && !value.empty()) {
    m_tpxoGridSpacing->SetValue(value);
  }
  if (config->Read("tpxo_cache_file", &value) && !value.empty()) {
    m_tpxoCacheFile->SetPath(value);
  }
  m_useTpxoCache->SetValue(config->ReadBool("use_tpxo_cache", false));
  m_durationHours->SetValue(static_cast<int>(config->ReadLong("duration_hours", m_durationHours->GetValue())));
  m_stepHours->SetValue(static_cast<int>(config->ReadLong("step_hours", m_stepHours->GetValue())));
  bool rememberUsername = config->ReadBool("remember_copernicus_username", false);
  m_rememberUsername->SetValue(rememberUsername);
  if (rememberUsername && config->Read("copernicus_username", &value)) {
    m_username->SetValue(value);
  }
  config->SetPath(oldPath);
}

void CurrentGribDialog::SaveSettings() {
  wxConfigBase* config = wxConfigBase::Get(false);
  if (!config) return;
  wxString oldPath = config->GetPath();
  config->SetPath("/PlugIns/currentgrib_pi");
  config->Write("generation_mode", static_cast<long>(m_mode->GetSelection()));
  config->Write("tpxo_model_directory", m_tpxoModelDir->GetPath());
  config->Write("tpxo_model_name", m_tpxoModelName->GetValue());
  config->Write("tpxo_grid_spacing", m_tpxoGridSpacing->GetValue());
  config->Write("tpxo_cache_file", m_tpxoCacheFile->GetPath());
  config->Write("use_tpxo_cache", m_useTpxoCache->GetValue());
  config->Write("duration_hours", static_cast<long>(m_durationHours->GetValue()));
  config->Write("step_hours", static_cast<long>(m_stepHours->GetValue()));
  config->Write("remember_copernicus_username", m_rememberUsername->GetValue());
  if (m_rememberUsername->GetValue()) {
    config->Write("copernicus_username", m_username->GetValue());
  } else {
    config->DeleteEntry("copernicus_username", false);
  }
  config->Flush();
  config->SetPath(oldPath);
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
