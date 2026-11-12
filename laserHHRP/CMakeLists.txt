cmake_minimum_required(VERSION 3.16)
project(laser_hhrp)

if(NOT CMAKE_CXX_STANDARD)
  set(CMAKE_CXX_STANDARD 17)
endif()
add_compile_options(-Wall -Wextra -Wpedantic)

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(rclcpp REQUIRED)
find_package(std_msgs REQUIRED)
find_package(std_srvs REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(ament_index_cpp REQUIRED)

# ---- messages ----------------------------------------------------------------------------------
file(GLOB MSG_FILES RELATIVE ${CMAKE_CURRENT_SOURCE_DIR} msg/*.msg)
rosidl_generate_interfaces(${PROJECT_NAME} ${MSG_FILES} DEPENDENCIES std_msgs geometry_msgs)
rosidl_get_typesupport_target(MSGS_TARGET ${PROJECT_NAME} rosidl_typesupport_cpp)

# ---- scanCONTROL SDK (installed separately, see README) ----------------------------------------
find_package(PkgConfig REQUIRED)
pkg_check_modules(LLT IMPORTED_TARGET llt)
if(LLT_FOUND)
  message(STATUS "scanCONTROL SDK found: building the real laser driver")
else()
  message(WARNING "scanCONTROL SDK (libllt) not found: laser_driver_node is built without it "
                  "(use simulate:=true, or install the SDK and rebuild)")
endif()

# ---- nodes -------------------------------------------------------------------------------------
function(add_node name)
  add_executable(${name} src/${name}.cpp ${ARGN})
  target_include_directories(${name} PRIVATE include)
  target_link_libraries(${name} ${MSGS_TARGET})
  ament_target_dependencies(${name} rclcpp std_msgs std_srvs sensor_msgs geometry_msgs)
  install(TARGETS ${name} DESTINATION lib/${PROJECT_NAME})
endfunction()

add_node(nano_driver_node)
add_node(laser_driver_node src/scancontrol.cpp)
add_node(acquisition_node)
add_node(odometry_node)
add_node(synchronizer_node)
add_node(point_cloud_node)
add_node(inspection_node)
add_node(recorder_node)
add_node(calibration_node)
add_node(scan_controller_node)
add_node(simulator_node)

ament_target_dependencies(laser_driver_node ament_index_cpp)  # finds laser.config_file

if(LLT_FOUND)
  target_compile_definitions(laser_driver_node PRIVATE LASER_HHRP_HAS_SDK=1)
  target_link_libraries(laser_driver_node PkgConfig::LLT)
  # llt.h includes Aravis/GLib inside extern "C"; including Aravis first avoids GLib's C++
  # templates ending up there.
  pkg_check_modules(ARAVIS REQUIRED IMPORTED_TARGET aravis-0.8)
  target_link_libraries(laser_driver_node PkgConfig::ARAVIS)
  target_compile_options(laser_driver_node PRIVATE "SHELL:-include arv.h")
endif()

# ---- GUI: RViz operator panel (optional: needs rviz_common) ------------------------------------
find_package(rviz_common QUIET)
find_package(pluginlib QUIET)
find_package(Qt5 QUIET COMPONENTS Widgets)
if(rviz_common_FOUND AND pluginlib_FOUND AND Qt5_FOUND)
  set(CMAKE_AUTOMOC ON)
  add_library(laser_hhrp_gui SHARED src/gui/operator_panel.cpp src/gui/operator_panel.hpp)
  target_include_directories(laser_hhrp_gui PRIVATE include)
  target_link_libraries(laser_hhrp_gui ${MSGS_TARGET} Qt5::Widgets)
  ament_target_dependencies(laser_hhrp_gui rclcpp rviz_common pluginlib std_srvs)
  pluginlib_export_plugin_description_file(rviz_common plugins.xml)
  install(TARGETS laser_hhrp_gui LIBRARY DESTINATION lib)
else()
  message(WARNING "rviz_common not found: the operator panel is not built")
endif()

# ---- other files -------------------------------------------------------------------------------
install(DIRECTORY launch config DESTINATION share/${PROJECT_NAME})
install(PROGRAMS scripts/read_session.py scripts/check_session.py scripts/timed_scan.py scripts/scan_switch_node.py
        DESTINATION lib/${PROJECT_NAME})

# ---- tests -------------------------------------------------------------------------------------
if(BUILD_TESTING)
  find_package(ament_cmake_gtest REQUIRED)
  ament_add_gtest(test_logic test/test_logic.cpp)
  target_include_directories(test_logic PRIVATE include)
  target_link_libraries(test_logic ${MSGS_TARGET})
  ament_target_dependencies(test_logic rclcpp)
endif()

ament_package()
