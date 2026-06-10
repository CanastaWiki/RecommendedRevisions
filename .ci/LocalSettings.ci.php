<?php
/**
 * LocalSettings.php for CI testing of RecommendedRevisions.
 *
 * This file is auto-mounted into the MediaWiki container by docker-compose.
 * Extension/skin load lines are appended at the bottom by install_extensions.sh.
 */

# ── Core settings ────────────────────────────────────────────────────────────

$wgSitename = "RecommendedRevisions CI";
$wgServer = getenv('MW_SERVER') ?: "http://localhost:8080";
$wgScriptPath = getenv('MW_SCRIPT_PATH') !== false ? getenv('MW_SCRIPT_PATH') : "";

$wgArticlePath = "$wgScriptPath/$1";

# ── Database ─────────────────────────────────────────────────────────────────

$wgDBtype = getenv('MW_DB_TYPE') ?: "mysql";
$wgDBserver = getenv('MW_DB_SERVER') ?: "db";
$wgDBname = getenv('MW_DB_NAME') ?: "mediawiki";
$wgDBuser = getenv('MW_DB_USER') ?: "wiki";
$wgDBpassword = getenv('MW_DB_PASS') ?: "wiki_password";
$wgDBprefix = "";
$wgDBTableOptions = "ENGINE=InnoDB, DEFAULT CHARSET=binary";

# ── Paths / uploads ─────────────────────────────────────────────────────────

$wgEnableUploads = true;
$wgUploadDirectory = "{$IP}/images";
$wgUseImageMagick = false;

# ── Development / debugging ──────────────────────────────────────────────────

$wgShowExceptionDetails = true;
$wgShowDBErrorBacktrace = true;
$wgDevelopmentWarnings = true;
$wgDeprecationReleaseLimit = false;

error_reporting( E_ALL );
ini_set( 'display_errors', '1' );

# ── Secrets (throwaway CI values) ────────────────────────────────────────────

$wgSecretKey = "ci-secret-key-not-for-production-use-0123456789abcdef0123456789abcdef";
$wgUpgradeKey = "ci-upgrade-key";

# ── Performance ──────────────────────────────────────────────────────────────

$wgMainCacheType = CACHE_NONE;
$wgCacheDirectory = false;
$wgObjectCaches[CACHE_DB] = [ 'class' => SqlBagOStuff::class ];
$wgJobRunRate = 0;

# ── Permissions (allow install script to run update.php) ─────────────────────

$wgGroupPermissions['*']['read'] = true;
$wgGroupPermissions['*']['edit'] = false;

# ── Extensions & skins ──────────────────────────────────────────────────────
# Extension/skin load lines are generated dynamically per test run by
# .ci/run_isolated_tests.py. Each extension is tested in isolation with
# only its declared dependencies loaded.
#
# For co-existence group tests and legacy all-in-one mode, load lines are
# appended below this point by the install script.

# ── Compatibility class aliases ──────────────────────────────────────────────
# Workaround for extensions written for newer MediaWiki versions that expect namespaced core classes.
# Avoid defining the alias if CrawlerProtection is loaded, as it unconditionally defines it.
$isCrawlerProtection = false;
if ( file_exists( __FILE__ ) ) {
    $isCrawlerProtection = strpos( file_get_contents( __FILE__ ), 'CrawlerProtection' ) !== false;
}
if ( !$isCrawlerProtection && !class_exists( 'MediaWiki\Page\Article' ) && class_exists( 'Article' ) ) {
    class_alias( 'Article', 'MediaWiki\Page\Article' );
}


# ── Test autoloading for SMW extensions ──────────────────────────────────────
# Autoload SMW test classes (like JsonTestCaseScriptRunnerTest) for dependent extensions.
spl_autoload_register( function ( $class ) {
    if ( strpos( $class, 'SMW\\Tests\\' ) === 0 ) {
        $relPath = str_replace( '\\', '/', substr( $class, 10 ) ) . '.php';
        $file = $GLOBALS['IP'] . '/extensions/SemanticMediaWiki/tests/phpunit/' . $relPath;
        if ( file_exists( $file ) ) {
            require_once $file;
        }
    }
} );
