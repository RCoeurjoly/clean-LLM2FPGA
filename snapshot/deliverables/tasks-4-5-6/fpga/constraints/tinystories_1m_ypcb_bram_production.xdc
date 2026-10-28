set_property PACKAGE_PIN AA28 [get_ports clk]
set_property IOSTANDARD LVCMOS18 [get_ports clk]
# The physical AA28 oscillator is 50 MHz; the top's MMCM generates 30 MHz.
create_clock -period 20.000 [get_ports clk]
create_clock -period 33.333 [get_nets accelerator_clk]

set_property PACKAGE_PIN P30 [get_ports {led[0]}]
set_property PACKAGE_PIN M30 [get_ports {led[1]}]
set_property PACKAGE_PIN N30 [get_ports {led[2]}]
set_property IOSTANDARD LVCMOS18 [get_ports {led[0]}]
set_property IOSTANDARD LVCMOS18 [get_ports {led[1]}]
set_property IOSTANDARD LVCMOS18 [get_ports {led[2]}]
