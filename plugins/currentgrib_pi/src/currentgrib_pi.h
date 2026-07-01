#pragma once

#include <wx/wx.h>
#include "ocpn_plugin.h"

class CurrentGribDialog;

class currentgrib_pi : public opencpn_plugin_118 {
public:
  explicit currentgrib_pi(void* ppimgr);
  ~currentgrib_pi() override;

  int Init() override;
  bool DeInit() override;
  int GetAPIVersionMajor() override;
  int GetAPIVersionMinor() override;
  int GetPlugInVersionMajor() override;
  int GetPlugInVersionMinor() override;
  wxBitmap* GetPlugInBitmap() override;
  wxString GetCommonName() override;
  wxString GetShortDescription() override;
  wxString GetLongDescription() override;
  int GetToolbarToolCount() override;
  void OnToolbarToolCallback(int id) override;
  void SetCurrentViewPort(PlugIn_ViewPort& vp) override;

private:
  void LoadIconBitmap();
  wxString FindIconPath() const;

  wxWindow* m_parent;
  int m_toolId;
  wxBitmap m_icon;
  CurrentGribDialog* m_dialog;
  bool m_hasCurrentViewPort{false};
  PlugIn_ViewPort m_currentViewPort{};
};
