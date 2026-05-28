LabSmith Control — Windows packaging notes
============================================

1) Install dependencies (in an environment where labsmith_gui.py already runs):
   pip install pyinstaller pyserial PyQt6 numpy

2) Ensure Python/uProcess_x64/ exists and contains uProcess_x64.pyd (and vendor DLLs).
   The spec bundles the whole uProcess_x64 folder into the exe.

3) From the Python directory run:
   cd Python
   pyinstaller --noconfirm packaging/LabSmithControl.spec

4) Output:
   Python/dist/LabSmithControl.exe   (no console window)

5) Log directory:
   logs/OUTPUT.txt next to the exe
   If you install under Program Files or another read-only location, set:
   LABSMITH_DATA_DIR=C:\Users\YourName\AppData\Local\LabSmithControl
   then start the exe; logs go under logs/ in that folder.

6) Custom icon:
   Put icon.ico under packaging/, then edit LabSmithControl.spec and uncomment the icon= line.

7) macOS:
   You usually need to adjust console=False or use a separate .app workflow; on Mac, use pyinstaller
   with --windowed for a .app and handle code signing (unsigned builds may be blocked by Gatekeeper).
