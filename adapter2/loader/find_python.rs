use crate::Error;
use loading::*;
use std::ffi::CStr;
use std::mem::transmute;
use std::os::raw::c_char;
use std::path::PathBuf;

pub fn find_python() -> Result<PathBuf, Error> {
    let locations = get_candidate_locations();
    for path in locations {
        unsafe {
            match load_library(&path, true) {
                Ok(handle) => {
                    if let Ok(ptr) = find_symbol(handle, "Py_GetVersion") {
                        let py_getversion: unsafe extern "C" fn() -> *const c_char = transmute(ptr);
                        let version = CStr::from_ptr(py_getversion());
                        if let Ok(version) = version.to_str() {
                            if let Ok((major, minor)) = parse_version(version) {
                                // Unlikely that Python 4 will be compatible.
                                if major == 3 && minor >= 3 {
                                    free_library(handle)?;
                                    return Ok(path);
                                }
                            }
                        }
                    }
                    free_library(handle)?;
                }
                Err(_) => {}
            }
        }
    }
    Err("No suitable Python 3 installation found.".into())
}

#[cfg(target_os = "linux")]
fn get_candidate_locations() -> Vec<PathBuf> {
    use std::io::{BufRead, BufReader};

    fn query_sysconfig() -> Result<PathBuf, Error> {
        let result = std::process::Command::new("python3")
            .arg("-c")
            .arg("import sysconfig; print(sysconfig.get_config_var('INSTSONAME'))")
            .output()?;
        if !result.status.success() {
            return Err(format!("python exit code: {:?}", result.status.code()).into());
        }
        let stdout = BufReader::new(&result.stdout[..]);
        let mut lines = stdout.lines();
        let path = PathBuf::from(lines.next().unwrap()?);
        Ok(path)
    }

    match query_sysconfig() {
        Ok(path) => vec![path],
        Err(err) => {
            eprintln!("{}", err);
            vec![]
        }
    }
}

#[cfg(target_os = "freebsd")]
fn get_candidate_locations() -> Vec<PathBuf> {
    use std::io::{BufRead, BufReader};

    fn query_sysconfig() -> Result<PathBuf, Error> {
        let result = std::process::Command::new("python3")
            .arg("-c")
            .arg("import sysconfig; print(sysconfig.get_config_var('INSTSONAME'))")
            .output()?;
        if !result.status.success() {
            return Err(format!("python exit code: {:?}", result.status.code()).into());
        }
        let stdout = BufReader::new(&result.stdout[..]);
        let mut lines = stdout.lines();
        let path = PathBuf::from(lines.next().unwrap()?);
        Ok(path)
    }

    match query_sysconfig() {
        Ok(path) => vec![path],
        Err(err) => {
            eprintln!("{}", err);
            vec![]
        }
    }
}

#[cfg(target_os = "macos")]
fn get_candidate_locations() -> Vec<PathBuf> {
    use std::io::{BufRead, BufReader};

    fn query_sysconfig() -> Result<Vec<PathBuf>, Error> {
        let result = std::process::Command::new("python3")
            .arg("-c")
            .arg("import sys,sysconfig; print(sys.base_exec_prefix); print(sysconfig.get_config_var('INSTSONAME'))")
            .output()?;
        if !result.status.success() {
            return Err(format!("python exit code: {:?}", result.status.code()).into());
        }
        let stdout = BufReader::new(&result.stdout[..]);
        let mut lines = stdout.lines();

        let prefix = lines.next().unwrap()?; // e.g. '/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.7'
        let libname = lines.next().unwrap()?; // e.g. 'Python3.framework/Versions/3.7/Python3'

        let mut results = vec![];

        let mut path = PathBuf::from(&prefix);
        path.push("Python3");
        results.push(path);

        let mut path = PathBuf::from(&prefix);
        path.push("Python");
        results.push(path);

        let mut path = PathBuf::from(&prefix);
        path.pop();
        path.pop();
        path.pop();
        path.push(&libname);
        results.push(path);

        results.push(PathBuf::from(&libname));

        Ok(results)
    }

    match query_sysconfig() {
        Ok(results) => results,
        Err(err) => {
            eprintln!("{}", err);
            vec![]
        }
    }
}

#[cfg(target_os = "windows")]
fn get_candidate_locations() -> Vec<PathBuf> {
    use winreg::enums::*;

    let mut results = vec![];

    if let Ok(python_home) = std::env::var("PYTHONHOME") {
        let mut path = PathBuf::from(python_home);
        path.push("python3.dll");
        results.push(path);
    }

    for hive in &[HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE] {
        if let Ok(hk_python) = winreg::RegKey::predef(*hive).open_subkey("Software\\Python\\PythonCore") {
            for ver_tag in hk_python.enum_keys() {
                if let Ok(ver_tag) = ver_tag {
                    if let Ok(hk_version) = hk_python.open_subkey(ver_tag) {
                        if let Ok(hk_install_path) = hk_version.open_subkey("InstallPath") {
                            if let Ok(install_path) = hk_install_path.get_value::<String, _>("") {
                                let mut path = PathBuf::from(install_path);
                                path.push("python3.dll");
                                results.push(path);
                            }
                        }
                    }
                }
            }
        }
    }
    results
}

fn parse_version(version: &str) -> Result<(u32, u32), Error> {
    let mut parts = version.split(|c| !char::is_digit(c, 10));
    let major = parts.next().ok_or("None")?.parse::<u32>()?;
    let minor = parts.next().ok_or("None")?.parse::<u32>()?;
    Ok((major, minor))
}

#[test]
fn test_parse_version() {
    macro_rules! assert_match(($e:expr, $p:pat) => { assert!(match $e { $p => true, _ => false }, stringify!($e ~ $p)) });

    assert_match!(parse_version(""), Err(_));
    assert_match!(parse_version("1."), Err(_));
    assert_match!(parse_version("1.2"), Ok((1, 2)));
    assert_match!(parse_version("1.2.3.4"), Ok((1, 2)));
    assert_match!(parse_version("12.34"), Ok((12, 34)));
    assert_match!(parse_version("3.14rc1"), Ok((3, 14)));
}
