#Be very careful when adding patterns in this array, as a missing comma can
#break the context generation/analysis
EXCLUDE_PATTERNS = [
    # Log files
    "*.log",

    # Documents for kiro
    "*.md",
    "*.hook",
    ".kiro/*",

    #######################################################
    ## Frontend projects
    #######################################################
    # Image files
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.ico",
    "*.bmp",
    "*.svg",
    "*.tiff",
    "*.webp",
    "assets/*",

    # Tests in ts/js projects
    "*jest*",
    "*.test.js",
    "*.spec.js",
    "*.test.ts",
    "*.spec.ts",
    "__tests__/*",
    "__mocks__/*",
    "mocks/*",
    "reports/coverage/*",

    # type files in ts/js projects
    "types/*",
    "*.d.ts",

    #dependencies files for ts/js projects
    "node_modules/*",
    "package-lock.json",
    "pnpm-lock.yaml",

    #build files for ts/js projects
    "build/*",
    "dist/*",
    ".next/*",
    ".webpack/*",
    "tsconfig.tsbuildinfo",

    # additional configuration for js/ts projects
    ".eslintrc.json",
    ".eslintrc",
    "i18n**",
    "tailwind.config.js",
    "tailwind.config.ts",
    ".prettierignore",

    # environment variables
    ".env*",

    #######################################################
    ## Backend projects
    #######################################################
    # tests in java
    "test/*",

    # build files in java
    "target/*",

    # java settings
    ".mvn/*",
    ".gradle/*",
    "gradle/*",
    "gradlew",
    "gradlew.bat",
    "*.jar",
    "*.war",

    # python files
    "__pycache__/*",
    ".venv/*",

    # complementary yamls for the project
    "docker-images/*",
    "helm/*",

    # vscode settings
    ".vscode/*",

    # git/gitlab configuration
    ".git/*",
    ".gitlab/*",
    ".gitignore",

    # macOs files
    ".DS_Store",

    # rwtt ai onboarding files
    ".scripts/*",
    "scr-input.sh",

    # old files (for manual analysis and/or setup)
    "*.old",
    ]