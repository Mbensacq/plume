//! Intègre l'icône et la description de Plume dans l'exécutable Windows.

fn main() {
    println!("cargo:rerun-if-changed=assets/plume.ico");
    println!("cargo:rerun-if-changed=build.rs");
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("windows") {
        return;
    }
    let mut res = winresource::WindowsResource::new();
    res.set_icon("assets/plume.ico")
        .set("FileDescription", "Plume — dictée vocale")
        .set("ProductName", "Plume")
        .set("OriginalFilename", "plume.exe");
    // Sans outil de ressources, l'exécutable fonctionne quand même (sans icône).
    if let Err(e) = res.compile() {
        println!("cargo:warning=icône non intégrée : {e}");
    }
}
