# this file uses pyinstaller to create the binary tarball that is used to deploy the tool binary
# this allows the tool binary to be deployed without installing python and other required python packages
#
TOOL=hw_monitor
MAIN=$TOOL.py
TARGET=tarball/$TOOL

pyinstaller --onefile $MAIN

mkdir -p $TARGET
cp dist/$TOOL $TARGET

cd tarball
tar cvzf ../${TOOL}.tar $TOOL
