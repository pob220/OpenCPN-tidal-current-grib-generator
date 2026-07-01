#pragma once

#include <wx/filepicker.h>
#include <wx/process.h>
#include <wx/spinctrl.h>
#include <wx/timer.h>
#include <wx/wx.h>

#include "ocpn_plugin.h"

class CurrentGribDialog : public wxDialog {
public:
  explicit CurrentGribDialog(wxWindow* parent);
  ~CurrentGribDialog() override;
  void SetCurrentViewPort(const PlugIn_ViewPort& vp);

private:
  void OnCheckDependencies(wxCommandEvent& event);
  void OnGenerate(wxCommandEvent& event);
  void OnBrowseOutput(wxCommandEvent& event);
  void OnPresetChanged(wxCommandEvent& event);
  void OnProviderChanged(wxCommandEvent& event);
  void OnCancel(wxCommandEvent& event);
  void OnClose(wxCommandEvent& event);
  void OnDialogClose(wxCloseEvent& event);
  void OnProcessTimer(wxTimerEvent& event);
  void OnProcessTerminated(wxProcessEvent& event);
  void AppendLog(const wxString& message);
  void DrainProcessOutput();
  void FlushProcessOutput();
  void DrainStream(wxInputStream* stream, wxString* buffer, const wxString& prefix);
  void StartCommand(const wxString& command, const wxString& password, bool generation);
  void FinishCommand(long exit_code, bool launched);
  void SetBusy(bool busy);
  void ApplyPreset(int selection);
  bool ConfirmLargeCopernicusRequest();
  bool AutoWouldUseMarineIe() const;
  void UpdateProviderUi();
  void TryOpenGeneratedGrib();
  wxString BuildGenerateCommand() const;
  wxString OutputPath() const;
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
  wxChoice* m_presetChoice;
  wxChoice* m_provider;
  wxTextCtrl* m_username;
  wxTextCtrl* m_password;
  wxCheckBox* m_rememberUsername;
  wxStaticText* m_providerNote;
  wxFilePickerCtrl* m_localNetcdf;
  wxDirPickerCtrl* m_outputDir;
  wxTextCtrl* m_outputFile;
  wxButton* m_checkButton;
  wxButton* m_generateButton;
  wxButton* m_cancelButton;
  wxButton* m_closeButton;
  wxCheckBox* m_openAfter;
  wxCheckBox* m_showMergeInstructions;
  wxTextCtrl* m_log;
  wxTimer m_processTimer;
  wxProcess* m_process{nullptr};
  bool m_processRunning{false};
  bool m_processGeneration{false};
  bool m_processCancelled{false};
  long m_processPid{0};
  bool m_hasCurrentViewPort{false};
  PlugIn_ViewPort m_currentViewPort{};
  wxString m_currentCommand;
  wxString m_stdoutBuffer;
  wxString m_stderrBuffer;
};
