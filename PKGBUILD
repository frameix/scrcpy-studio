# Maintainer: frameix <marc77mz@gmail.com>
pkgname=scrcpy-studio
pkgver=1.0.0
pkgrel=1
pkgdesc="Modern Wayland-native GTK4/Libadwaita Studio and Controller for Android via scrcpy"
arch=('any')
url="https://github.com/frameix/scrcpy-studio"
license=('MIT')
depends=(
    'python'
    'python-gobject'
    'gtk4'
    'libadwaita'
    'gstreamer'
    'gst-plugins-base'
    'gst-plugins-good'
    'scrcpy'
    'android-tools'
)
optdepends=(
    'v4l2loopback-dkms: Virtual webcam output support'
    'gst-plugins-bad: Hardware video decoding acceleration'
    'hyprland: Dynamic aspect-ratio window management on Wayland'
)
makedepends=(
    'python-build'
    'python-installer'
    'python-wheel'
    'python-setuptools'
)
source=()
sha256sums=()

build() {
    cd "${startdir}"
    python -m build --wheel --no-isolation
}

package() {
    cd "${startdir}"
    python -m installer --destdir="$pkgdir" dist/*.whl
    install -Dm644 assets/scrcpy-studio.desktop "$pkgdir/usr/share/applications/scrcpy-studio.desktop"
    install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
    install -Dm644 README.md "$pkgdir/usr/share/doc/$pkgname/README.md"
}
