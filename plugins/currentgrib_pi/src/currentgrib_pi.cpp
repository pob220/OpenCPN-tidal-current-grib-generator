#include "currentgrib_pi.h"
#include "currentgrib_dialog.h"

#include <wx/filename.h>

extern "C" DECL_EXP opencpn_plugin* create_pi(void* ppimgr) {
  return new currentgrib_pi(ppimgr);
}

extern "C" DECL_EXP void destroy_pi(opencpn_plugin* p) { delete p; }

currentgrib_pi::currentgrib_pi(void* ppimgr)
    : opencpn_plugin_118(ppimgr), m_parent(nullptr), m_toolId(-1), m_dialog(nullptr) {}

currentgrib_pi::~currentgrib_pi() {}

int currentgrib_pi::Init() {
  m_parent = GetOCPNCanvasWindow();
  wxString shareLocn = *GetpSharedDataLocation() + "plugins" + wxFileName::GetPathSeparator() +
                       "currentgrib_pi" + wxFileName::GetPathSeparator() + "data" +
                       wxFileName::GetPathSeparator();
  wxImage image(shareLocn + "currentgrib.png");
  if (image.IsOk()) {
    image.Rescale(32, 32, wxIMAGE_QUALITY_HIGH);
    m_icon = wxBitmap(image);
  }
  m_toolId = InsertPlugInTool("", &m_icon, &m_icon, wxITEM_NORMAL,
                              "Ocean Current GRIB Generator", "", nullptr, -1, 0, this);
  return WANTS_TOOLBAR_CALLBACK | INSTALLS_TOOLBAR_TOOL;
}

bool currentgrib_pi::DeInit() {
  if (m_dialog) {
    m_dialog->Destroy();
    m_dialog = nullptr;
  }
  return true;
}

int currentgrib_pi::GetAPIVersionMajor() { return 1; }
int currentgrib_pi::GetAPIVersionMinor() { return 18; }
int currentgrib_pi::GetPlugInVersionMajor() { return 0; }
int currentgrib_pi::GetPlugInVersionMinor() { return 1; }
wxBitmap* currentgrib_pi::GetPlugInBitmap() { return &m_icon; }
wxString currentgrib_pi::GetCommonName() { return "Current GRIB Generator"; }
wxString currentgrib_pi::GetShortDescription() { return "Generate ocean-current GRIB files"; }
wxString currentgrib_pi::GetLongDescription() {
  return "Downloads or converts modelled ocean-current data into OpenCPN-compatible current GRIB files.";
}
int currentgrib_pi::GetToolbarToolCount() { return 1; }

void currentgrib_pi::OnToolbarToolCallback(int) {
  if (!m_dialog) {
    m_dialog = new CurrentGribDialog(m_parent);
  }
  m_dialog->Show();
  m_dialog->Raise();
}
