package com.immanuelc0d5s.shield.db

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import android.util.Log
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * Generates and stores the SQLCipher passphrase.
 *
 * The pattern is the standard Android one: generate a random passphrase, wrap it
 * with an Android Keystore key that never leaves the TEE, and store only the
 * wrapped form in SharedPreferences. The unwrapped passphrase exists in memory
 * only while the database is being opened.
 *
 * Why this rather than a hardcoded passphrase in the source: a constant in the
 * APK protects nothing, since `strings` extracts it from any build. Why not
 * `EncryptedSharedPreferences`: it also needs a Keystore key, and adding the
 * `androidx.security` dependency for one value that is only read at open time is
 * not worth the extra library.
 */
object DatabaseKey {
    private const val TAG = "DatabaseKey"
    private const val KEYSTORE = "AndroidKeyStore"
    private const val KEY_ALIAS = "shield-db-key"
    private const val PREFS = "shield-db-key-prefs"
    private const val PREF_WRAPPED = "wrapped_passphrase"
    private const val PREF_IV = "wrapped_iv"

    /** 256-bit. SQLCipher's own default cipher, PBKDF2-HMAC-SHA512. */
    private const val PASS_BYTES = 32
    private const val TRANSFORMATION = "AES/GCM/NoPadding"
    private const val GCM_TAG_BITS = 128

    @Volatile
    private var cached: ByteArray? = null

    /**
     * The passphrase as raw bytes, ready for `SupportOpenHelperFactory`.
     *
     * `cleartext()` on the SQLCipher side accepts a byte array directly and does
     * not put it through `String`, which is the only representation Android cannot
     * reliably zero.
     */
    fun getOrCreate(context: Context): ByteArray {
        cached?.let { return it }
        return synchronized(this) {
            cached ?: load(context).also { cached = it }
        }
    }

    private fun load(context: Context): ByteArray {
        val prefs = context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val wrapped = prefs.getString(PREF_WRAPPED, null)
        val iv = prefs.getString(PREF_IV, null)

        if (wrapped != null && iv != null) {
            runCatching {
                return unwrap(Base64.decode(wrapped, Base64.NO_WRAP), Base64.decode(iv, Base64.NO_WRAP))
            }.onFailure {
                // Most likely: the user changed screen lock or the keystore entry
                // was invalidated, which destroys the key. Falling back to a new
                // passphrase would silently produce an *empty* database that looks
                // like "no events yet" - destroying the audit trail and showing
                // nothing. Fail loudly instead.
                throw IllegalStateException(
                    "the stored event-log key cannot be unwrapped; the Keystore entry was " +
                        "probably invalidated by a screen-lock change. Clearing app data will " +
                        "recover the app but discards the local event log.",
                    it,
                )
            }
        }

        return create(context, prefs)
    }

    private fun create(context: Context, prefs: android.content.SharedPreferences): ByteArray {
        val passphrase = ByteArray(PASS_BYTES).also { java.security.SecureRandom().nextBytes(it) }
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, getOrCreateKey())

        val wrapped = cipher.doFinal(passphrase)
        prefs.edit()
            .putString(PREF_WRAPPED, Base64.encodeToString(wrapped, Base64.NO_WRAP))
            .putString(PREF_IV, Base64.encodeToString(cipher.iv, Base64.NO_WRAP))
            .apply()

        Log.i(TAG, "generated a new $PASS_BYTES-byte SQLCipher passphrase")
        return passphrase
    }

    private fun unwrap(wrapped: ByteArray, iv: ByteArray): ByteArray {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(
            Cipher.DECRYPT_MODE,
            getOrCreateKey(),
            GCMParameterSpec(GCM_TAG_BITS, iv),
        )
        return cipher.doFinal(wrapped)
    }

    private fun getOrCreateKey(): SecretKey {
        val keyStore = KeyStore.getInstance(KEYSTORE).apply { load(null) }
        (keyStore.getEntry(KEY_ALIAS, null) as? KeyStore.SecretKeyEntry)?.let { return it.secretKey }

        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE)
        generator.init(
            KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                // Deliberately not requiring user authentication. The event log has
                // to be readable while the phone is locked, which is exactly when
                // this app is in use. The keystore key still cannot be extracted.
                .setRandomizedEncryptionRequired(true)
                .build(),
        )
        return generator.generateKey()
    }
}