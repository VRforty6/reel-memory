package dev.reelmemory.app.data

import android.os.Build

/** Keeps Android runtime classification in one place and makes its rules unit-testable. */
internal object AndroidDeviceEnvironment {
    fun isEmulator(): Boolean = isEmulator(
        fingerprint = Build.FINGERPRINT,
        model = Build.MODEL,
        manufacturer = Build.MANUFACTURER,
        brand = Build.BRAND,
        device = Build.DEVICE,
        product = Build.PRODUCT,
    )

    internal fun isEmulator(
        fingerprint: String?,
        model: String?,
        manufacturer: String?,
        brand: String?,
        device: String?,
        product: String?,
    ): Boolean {
        val normalizedFingerprint = fingerprint.orEmpty().lowercase()
        val normalizedModel = model.orEmpty().lowercase()
        val normalizedManufacturer = manufacturer.orEmpty().lowercase()
        val normalizedBrand = brand.orEmpty().lowercase()
        val normalizedDevice = device.orEmpty().lowercase()
        val normalizedProduct = product.orEmpty().lowercase()

        return normalizedFingerprint.startsWith("generic") ||
            normalizedFingerprint.startsWith("unknown") ||
            normalizedModel.contains("google_sdk") ||
            normalizedModel.contains("emulator") ||
            normalizedModel.contains("android sdk built for x86") ||
            normalizedManufacturer.contains("genymotion") ||
            (normalizedBrand.startsWith("generic") && normalizedDevice.startsWith("generic")) ||
            normalizedProduct.contains("sdk") ||
            normalizedProduct.contains("emulator") ||
            normalizedProduct.contains("simulator") ||
            normalizedProduct.contains("vbox86p")
    }
}
