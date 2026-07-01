cmake_minimum_required(VERSION 3.12)
project(currentgrib_pi)

set(PACKAGE_NAME currentgrib_pi)
set(VERBOSE_NAME "Current GRIB Generator")
set(TITLE_NAME "Current GRIB Generator")
option(CURRENTGRIB_INSTALL_DEV_METADATA
       "Install currentgrib_pi development metadata into the local OpenCPN private data directory"
       ON)

set(SRC
  src/currentgrib_pi.h
  src/currentgrib_pi.cpp
  src/currentgrib_dialog.h
  src/currentgrib_dialog.cpp
)

find_package(wxWidgets COMPONENTS core base REQUIRED)
include(${wxWidgets_USE_FILE})

add_library(${PACKAGE_NAME} SHARED ${SRC})
target_compile_features(${PACKAGE_NAME} PRIVATE cxx_std_17)
target_include_directories(${PACKAGE_NAME} PRIVATE src)

if (TARGET ocpn::opencpn)
  target_link_libraries(${PACKAGE_NAME} PRIVATE ocpn::opencpn)
else()
  set(OCPN_ROOT "" CACHE PATH "Path to OpenCPN source tree for standalone development builds")
  if (NOT OCPN_ROOT)
    message(FATAL_ERROR "Set OCPN_ROOT or build this plugin inside the OpenCPN source tree")
  endif()
  target_include_directories(${PACKAGE_NAME} PRIVATE ${OCPN_ROOT}/include)
endif()

target_link_libraries(${PACKAGE_NAME} PRIVATE ${wxWidgets_LIBRARIES})

if (CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
  target_compile_options(${PACKAGE_NAME} PRIVATE -Wno-deprecated-declarations)
endif()

add_custom_command(
  TARGET ${PACKAGE_NAME}
  POST_BUILD
  COMMAND ${CMAKE_COMMAND} -E make_directory "$<TARGET_FILE_DIR:${PACKAGE_NAME}>/data"
  COMMAND ${CMAKE_COMMAND} -E copy_if_different
          "${CMAKE_CURRENT_SOURCE_DIR}/data/currentgrib.png"
          "$<TARGET_FILE_DIR:${PACKAGE_NAME}>/data/currentgrib.png"
  COMMAND ${CMAKE_COMMAND} -E make_directory
          "${CMAKE_BINARY_DIR}/share/plugins/currentgrib_pi/data"
  COMMAND ${CMAKE_COMMAND} -E copy_if_different
          "${CMAKE_CURRENT_SOURCE_DIR}/data/currentgrib.png"
          "${CMAKE_BINARY_DIR}/share/plugins/currentgrib_pi/data/currentgrib.png"
)

if (CURRENTGRIB_INSTALL_DEV_METADATA AND UNIX AND NOT APPLE AND DEFINED ENV{HOME})
  add_custom_command(
    TARGET ${PACKAGE_NAME}
    POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E make_directory
            "$ENV{HOME}/.opencpn/plugins/install_data/imports"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${CMAKE_CURRENT_SOURCE_DIR}/data/currentgrib_pi.xml"
            "$ENV{HOME}/.opencpn/plugins/install_data/imports/currentgrib_pi.xml"
  )
endif()
