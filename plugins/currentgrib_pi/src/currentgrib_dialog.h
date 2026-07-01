#pragma once

#include <wx/wx.h>
#include <wx/filepicker.h>
#include <wx/spinctrl.h>

class CurrentGribDialog : public wxDialog {
public:
  explicit CurrentGribDialog(wxWindow* parent);

private:
  void OnCheckDependencies(wxCommandEvent& event);
  void OnGenerate(wxCommandEvent& event);
  void OnClose(wxCommandEvent& event);
  void AppendLog(const wxString& message);
  void RunCommandAndLog(const wxString& command);
  wxString BuildGenerateCommand() const;
  wxString FindDefaultGenerator() const;
  wxString Redact(const wxString& text) const;

  wxTextCtrl* m_generatorPath;
  wxTextCtrl* m_west;
  wxTextCtrl* m_south;
  wxTextCtrl* m_east;
  wxTextCtrl* m_north;
  wxTextCtrl* m_startUtc;
  wxSpinCtrl* m_durationHours;
  wxSpinCtrl* m_stepHours;
  wxChoice* m_provider;
  wxTextCtrl* m_username;
  wxTextCtrl* m_password;
  wxCheckBox* m_rememberUsername;
  wxFilePickerCtrl* m_localNetcdf;
  wxDirPickerCtrl* m_outputDir;
  wxTextCtrl* m_outputFile;
  wxCheckBox* m_openAfter;
  wxCheckBox* m_showMergeInstructions;
  wxTextCtrl* m_log;
};
