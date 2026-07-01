#include "currentgrib_pi.h"
#include "currentgrib_dialog.h"

#include <wx/filename.h>
#include <wx/log.h>
#include <wx/stdpaths.h>

extern "C" DECL_EXP opencpn_plugin* create_pi(void* ppimgr) {
  wxLogMessage("currentgrib_pi: create_pi");
  return new currentgrib_pi(ppimgr);
}

extern "C" DECL_EXP void destroy_pi(opencpn_plugin* p) {
  wxLogMessage("currentgrib_pi: destroy_pi");
  delete p;
}

currentgrib_pi::currentgrib_pi(void* ppimgr)
    : opencpn_plugin_118(ppimgr), m_parent(nullptr), m_toolId(-1), m_dialog(nullptr) {
  wxLogMessage("currentgrib_pi: constructor");
  LoadIconBitmap();
}

currentgrib_pi::~currentgrib_pi() { wxLogMessage("currentgrib_pi: destructor"); }

int currentgrib_pi::Init() {
  wxLogMessage("currentgrib_pi: Init start");
  m_parent = GetOCPNCanvasWindow();
  LoadIconBitmap();
  wxLogMessage("currentgrib_pi: inserting toolbar tool; icon ok=%d size=%dx%d",
               m_icon.IsOk(), m_icon.GetWidth(), m_icon.GetHeight());
  m_toolId = InsertPlugInTool("", &m_icon, &m_icon, wxITEM_NORMAL,
                              "Ocean Current GRIB Generator", "", nullptr, -1, 0, this);
  wxLogMessage("currentgrib_pi: toolbar tool id=%d", m_toolId);
  return WANTS_TOOLBAR_CALLBACK | INSTALLS_TOOLBAR_TOOL | WANTS_ONPAINT_VIEWPORT;
}

bool currentgrib_pi::DeInit() {
  wxLogMessage("currentgrib_pi: DeInit start");
  if (m_toolId >= 0) {
    wxLogMessage("currentgrib_pi: removing toolbar tool id=%d", m_toolId);
    RemovePlugInTool(m_toolId);
    m_toolId = -1;
  }
  if (m_dialog) {
    m_dialog->Destroy();
    m_dialog = nullptr;
  }
  wxLogMessage("currentgrib_pi: DeInit complete");
  return true;
}

int currentgrib_pi::GetAPIVersionMajor() {
  wxLogMessage("currentgrib_pi: GetAPIVersionMajor -> 1");
  return 1;
}
int currentgrib_pi::GetAPIVersionMinor() {
  wxLogMessage("currentgrib_pi: GetAPIVersionMinor -> 18");
  return 18;
}
int currentgrib_pi::GetPlugInVersionMajor() {
  wxLogMessage("currentgrib_pi: GetPlugInVersionMajor -> 0");
  return 0;
}
int currentgrib_pi::GetPlugInVersionMinor() {
  wxLogMessage("currentgrib_pi: GetPlugInVersionMinor -> 1");
  return 1;
}
wxBitmap* currentgrib_pi::GetPlugInBitmap() {
  if (!m_icon.IsOk()) LoadIconBitmap();
  wxLogMessage("currentgrib_pi: GetPlugInBitmap icon ok=%d size=%dx%d",
               m_icon.IsOk(), m_icon.GetWidth(), m_icon.GetHeight());
  return &m_icon;
}
wxString currentgrib_pi::GetCommonName() {
  wxLogMessage("currentgrib_pi: GetCommonName");
  return "Current GRIB Generator";
}
wxString currentgrib_pi::GetShortDescription() {
  wxLogMessage("currentgrib_pi: GetShortDescription");
  return "Generate ocean-current GRIB files";
}
wxString currentgrib_pi::GetLongDescription() {
  wxLogMessage("currentgrib_pi: GetLongDescription");
  return "Downloads or converts modelled ocean-current data into OpenCPN-compatible current GRIB files.";
}
int currentgrib_pi::GetToolbarToolCount() {
  wxLogMessage("currentgrib_pi: GetToolbarToolCount -> 1");
  return 1;
}

void currentgrib_pi::OnToolbarToolCallback(int) {
  wxLogMessage("currentgrib_pi: toolbar callback");
  if (!m_dialog) {
    wxLogMessage("currentgrib_pi: creating dialog");
    m_dialog = new CurrentGribDialog(m_parent);
    if (m_hasCurrentViewPort) m_dialog->SetCurrentViewPort(m_currentViewPort);
  }
  m_dialog->Show();
  m_dialog->Raise();
}

void currentgrib_pi::SetCurrentViewPort(PlugIn_ViewPort& vp) {
  m_currentViewPort = vp;
  m_hasCurrentViewPort = vp.bValid;
  if (m_dialog) m_dialog->SetCurrentViewPort(vp);
}

void currentgrib_pi::LoadIconBitmap() {
  wxString iconPath = FindIconPath();
  if (!iconPath.empty()) {
    wxImage image(iconPath, wxBITMAP_TYPE_PNG);
    if (image.IsOk()) {
      image.Rescale(32, 32, wxIMAGE_QUALITY_HIGH);
      m_icon = wxBitmap(image);
      wxLogMessage("currentgrib_pi: loaded icon %s", iconPath);
      return;
    }
    wxLogMessage("currentgrib_pi: failed to decode icon %s", iconPath);
  }

  wxLogMessage("currentgrib_pi: using generated fallback icon");
  wxBitmap fallback(32, 32);
  wxMemoryDC dc(fallback);
  dc.SetBackground(*wxTRANSPARENT_BRUSH);
  dc.Clear();
  dc.SetBrush(*wxLIGHT_GREY_BRUSH);
  dc.SetPen(*wxBLACK_PEN);
  dc.DrawRectangle(2, 2, 28, 28);
  dc.DrawLine(7, 21, 25, 11);
  dc.DrawLine(20, 9, 25, 11);
  dc.DrawLine(22, 16, 25, 11);
  dc.SelectObject(wxNullBitmap);
  m_icon = fallback;
}

wxString currentgrib_pi::FindIconPath() const {
  wxArrayString candidates;
  wxString separator = wxFileName::GetPathSeparator();

  if (GetpSharedDataLocation()) {
    candidates.Add(*GetpSharedDataLocation() + "plugins" + separator +
                   "currentgrib_pi" + separator + "data" + separator +
                   "currentgrib.png");
  }

  wxString cwd = wxGetCwd();
  candidates.Add(cwd + separator + "build" + separator + "plugins" + separator +
                 "currentgrib_pi" + separator + "data" + separator +
                 "currentgrib.png");
  candidates.Add(cwd + separator + "build" + separator + "share" + separator +
                 "plugins" + separator + "currentgrib_pi" + separator + "data" +
                 separator + "currentgrib.png");
  candidates.Add(wxGetHomeDir() + separator + "src" + separator + "OpenCPN" +
                 separator + "build" + separator + "plugins" + separator +
                 "currentgrib_pi" + separator + "data" + separator +
                 "currentgrib.png");
  candidates.Add(wxGetHomeDir() + separator + "src" + separator + "OpenCPN" +
                 separator + "build" + separator + "share" + separator +
                 "plugins" + separator + "currentgrib_pi" + separator + "data" +
                 separator + "currentgrib.png");

  for (const auto& candidate : candidates) {
    wxLogMessage("currentgrib_pi: checking icon path %s", candidate);
    if (wxFileName::FileExists(candidate)) return candidate;
  }
  return "";
}
