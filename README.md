# AI Science Hack 2026

## Setup

### 1. Clone this repo

```bash
git clone https://github.com/OrthoDim/Cereal-Delusion.git
cd Cereal-Delusion
```

### 2. Clone reference repositories

These are gitignored but needed locally for Claude Code context and the Elnora plugin:

```bash
git clone https://github.com/Elnora-AI/elnora-cli.git
git clone https://github.com/PyLabRobot/pylabrobot.git
```

### 3. Install Conda (if not already installed)

```bash
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O miniconda.sh
bash miniconda.sh -b -p $HOME/miniconda3
eval "$($HOME/miniconda3/bin/conda shell.bash hook)"
conda init
```

### 4. Create and activate the environment

```bash
conda env create -f environment.yml
conda activate clonebot-env
```

Key packages: PyLabRobot (lab automation), Elnora CLI (bioprotocol platform), Flask, NumPy, SQLAlchemy, and hardware drivers (pyserial, pyusb, pymodbus, opentrons). See `environment.yml` for the full list.

### 5. Authenticate with Elnora

```bash
elnora auth login
```

### 6. Claude Code

The Elnora CLI is configured as a Claude Code plugin via `.claude/settings.json`. Once the reference repos are cloned, Claude Code will automatically load the Elnora skills and can reference PyLabRobot conventions.
