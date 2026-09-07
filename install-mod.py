import configparser
import shutil
from pathlib import Path
from datetime import datetime


def InstallMod():
    #Check if config exists
    configPath = Path("config.ini")
    if not configPath.is_file():
        print("No config file found. Please copy config.example.ini and name the copy 'config.ini'")
        return
        
    #Read config file
    config = configparser.ConfigParser()
    config.read('config.ini')

    #Get and verify path
    localPath = config['Paths']['install_folder']
    if(localPath == "YOUR_PATH_HERE"):
        print("Please define the path to your RE1 installation in config.ini")
        return
    installPath = Path(localPath)
    print(installPath)
    if not installPath.exists():
        print("Invalid or nonexistent path")
        return
    bhd_exe = installPath / "bhd.exe"
    if not bhd_exe.is_file():
        print("Defined path does not contain bhd.exe")
        return
    
    #Get current build
    currentBuild = Path("Release/dinput8.dll")
    if not currentBuild.exists():
        print("No build found in Releases folder. Please build Release dll")
        return

    #Copy build
    shutil.copy2(currentBuild, installPath)
    modifiedTimestamp = currentBuild.stat().st_mtime
    modifiedDate = datetime.fromtimestamp(modifiedTimestamp)
    print(f"Dll generated at {modifiedDate} installed to {installPath}")
    return
    

if (__name__ == "__main__"):
    InstallMod()