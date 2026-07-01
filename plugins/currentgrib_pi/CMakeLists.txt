cmake_minimum_required(VERSION 3.12)
project(currentgrib_pi)

# Scaffold only. For a production build, integrate these sources into the
# official OpenCPN plugin template or an OpenCPN source-tree plugin build.

set(SRC
  src/currentgrib_pi.cpp
  src/currentgrib_dialog.cpp
)

add_library(currentgrib_pi SHARED ${SRC})
target_compile_features(currentgrib_pi PRIVATE cxx_std_17)
target_include_directories(currentgrib_pi PRIVATE src)

find_package(wxWidgets COMPONENTS core base REQUIRED)
include(${wxWidgets_USE_FILE})
target_link_libraries(currentgrib_pi PRIVATE ${wxWidgets_LIBRARIES})
