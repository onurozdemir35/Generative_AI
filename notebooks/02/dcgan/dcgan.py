import argparse
import datetime
import hashlib
import logging
import os
from pprint import pprint

import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision
import wandb
import yaml
from torch.utils.data import DataLoader
from torchvision import datasets, transforms
from tqdm import tqdm

# ------------------------------
# Logging Configuration
# ------------------------------
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler()
console_formatter = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s')
console_handler.setFormatter(console_formatter)
logger.addHandler(console_handler)


# ------------------------------
# Define the Generator Network (DCGAN)
# ------------------------------
class Generator(nn.Module):
    def __init__(self, config):
        super(Generator, self).__init__()
        self.latent_dim = config["latent_dim"]
        self.ngf = config["ngf"]
        self.nc = config["nc"]
        # DCGAN generator architecture
        self.main = nn.Sequential(
            # Input: latent vector Z (batch_size, latent_dim, 1, 1)
            nn.ConvTranspose2d(self.latent_dim, self.ngf * 8, 4, 1, 0, bias=False),
            nn.BatchNorm2d(self.ngf * 8),
            nn.ReLU(True),
            # State: (ngf*8) x 4 x 4
            nn.ConvTranspose2d(self.ngf * 8, self.ngf * 4, 4, 2, 1, bias=False),
            nn.BatchNorm2d(self.ngf * 4),
            nn.ReLU(True),
            # State: (ngf*4) x 8 x 8
            nn.ConvTranspose2d(self.ngf * 4, self.ngf * 2, 4, 2, 1, bias=False),
            nn.BatchNorm2d(self.ngf * 2),
            nn.ReLU(True),
            # State: (ngf*2) x 16 x 16
            nn.ConvTranspose2d(self.ngf * 2, self.ngf, 4, 2, 1, bias=False),
            nn.BatchNorm2d(self.ngf),
            nn.ReLU(True),
            # State: (ngf) x 32 x 32
            nn.ConvTranspose2d(self.ngf, self.nc, 4, 2, 1, bias=False),
            nn.Tanh()
            # Output: (nc) x 64 x 64
        )
    
    def forward(self, input):
        return self.main(input)


# ------------------------------
# Define the Discriminator Network (DCGAN)
# ------------------------------
class Discriminator(nn.Module):
    def __init__(self, config):
        super(Discriminator, self).__init__()
        self.ndf = config["ndf"]
        self.nc = config["nc"]
        # DCGAN discriminator architecture
        self.main = nn.Sequential(
            # Input: (nc) x 64 x 64
            nn.Conv2d(self.nc, self.ndf, 4, 2, 1, bias=False),
            nn.LeakyReLU(0.2, inplace=True),
            # State: (ndf) x 32 x 32
            nn.Conv2d(self.ndf, self.ndf * 2, 4, 2, 1, bias=False),
            nn.BatchNorm2d(self.ndf * 2),
            nn.LeakyReLU(0.2, inplace=True),
            # State: (ndf*2) x 16 x 16
            nn.Conv2d(self.ndf * 2, self.ndf * 4, 4, 2, 1, bias=False),
            nn.BatchNorm2d(self.ndf * 4),
            nn.LeakyReLU(0.2, inplace=True),
            # State: (ndf*4) x 8 x 8
            nn.Conv2d(self.ndf * 4, self.ndf * 8, 4, 2, 1, bias=False),
            nn.BatchNorm2d(self.ndf * 8),
            nn.LeakyReLU(0.2, inplace=True),
            # State: (ndf*8) x 4 x 4
            nn.Conv2d(self.ndf * 8, 1, 4, 1, 0, bias=False),
            nn.Sigmoid()
        )
    
    def forward(self, input):
        return self.main(input).view(-1, 1).squeeze(1)


# ------------------------------
# Helper: Generate a unique checkpoint folder based on key hyperparameters
# ------------------------------
def generate_checkpoint_folder(config):
    # Create a string from key hyperparameters
    key_params = f"{config['lr']}_{config['batch_size']}_{config['latent_dim']}_{config['ngf']}_{config['ndf']}"

    config_hash = hashlib.sha256(key_params.encode()).hexdigest()[:8]
    base_path = config.get("checkpoint_dir", "checkpoints")
    os.makedirs(base_path, exist_ok=True)
    folder_name = f"dcgan_lr{config['lr']}_bs{config['batch_size']}_latent{config['latent_dim']}_ngf{config['ngf']}_ndf{config['ndf']}_{config_hash}"
    folder_path = os.path.join(base_path, folder_name)
    os.makedirs(folder_path, exist_ok=True)
    
    # Set up a file handler for logging metadata in this folder
    log_file = os.path.join(folder_path, "experiment.log")
    file_handler = logging.FileHandler(log_file)
    file_formatter = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s')
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)
    
    logger.info(f"Checkpoint folder created at: {folder_path}")
    return folder_path


# ------------------------------
# Checkpoint Saving & Loading
# ------------------------------
def save_checkpoint(folder, generator, discriminator, optimizer_g, optimizer_d, epoch, global_step, config):
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    checkpoint_filename = f"ckpt_epoch_{epoch}_{timestamp}.pth"
    checkpoint_path = os.path.join(folder, checkpoint_filename)
    state = {
        "epoch": epoch,
        "global_step": global_step,
        "generator_state_dict": generator.state_dict(),
        "discriminator_state_dict": discriminator.state_dict(),
        "optimizer_g_state_dict": optimizer_g.state_dict(),
        "optimizer_d_state_dict": optimizer_d.state_dict(),
        "wandb_run_id": wandb.run.id if wandb.run else None,
        "config": config
    }
    torch.save(state, checkpoint_path)
    logger.info(f"Checkpoint saved at epoch {epoch} to {checkpoint_path}")
    
    config_path = os.path.join(folder, "config.yaml")
    with open(config_path, "w") as f:
        yaml.dump(config, f)
    logger.info(f"Configuration saved at {config_path}")
    
    # Log checkpoint as a WandB artifact with unique naming.
    artifact = wandb.Artifact(f"model-checkpoint-epoch-{epoch}-{timestamp}", type="model", description=f"Checkpoint at epoch {epoch}")
    artifact.add_file(checkpoint_path)
    wandb.log_artifact(artifact)


def load_checkpoint(folder, generator, discriminator, optimizer_g, optimizer_d, device, current_config):
    # Find all checkpoint files in the folder
    ckpt_files = [f for f in os.listdir(folder) if f.startswith("ckpt_epoch_") and f.endswith(".pth")]
    if not ckpt_files:
        logger.info("No checkpoint files found; starting fresh.")
        return 0, 0, None
    # Determine the latest checkpoint (highest epoch number)
    latest_ckpt = max(ckpt_files, key=lambda f: int(f.split("_")[-1].split(".")[0]))
    checkpoint_path = os.path.join(folder, latest_ckpt)
    state = torch.load(checkpoint_path, map_location=device)
    pprint(state.keys())
    
    checkpoint_config = state.get("config", {})

    if checkpoint_config != current_config:
        logger.warning("Configuration differences between checkpoint and current run:")
        for key in set(checkpoint_config.keys()).union(current_config.keys()):
            if checkpoint_config.get(key) != current_config.get(key):
                logger.warning(f"  {key}: checkpoint={checkpoint_config.get(key)}, current={current_config.get(key)}")
        logger.warning("Configuration mismatch. Skipping checkpoint loading.")
        return 0, 0, None
    
    generator.load_state_dict(state["generator_state_dict"])
    discriminator.load_state_dict(state["discriminator_state_dict"])
    optimizer_g.load_state_dict(state["optimizer_g_state_dict"])
    optimizer_d.load_state_dict(state["optimizer_d_state_dict"])

    start_epoch = state["epoch"] + 1
    global_step = state.get("global_step", 0)
    wandb_run_id = state.get("wandb_run_id", None)

    logger.info(f"Loaded checkpoint from {checkpoint_path}, resuming from epoch {start_epoch} with global_step {global_step}")

    return start_epoch, global_step, wandb_run_id


# ------------------------------
# Training Epoch Function
# ------------------------------
def train_epoch(generator, discriminator, optimizer_g, optimizer_d, dataloader, device, config, epoch, global_step):
    generator.train()
    discriminator.train()
    
    total_g_loss = 0.0
    total_d_loss = 0.0
    n_batches = 0
    
    progress_bar = tqdm(dataloader, desc=f"Epoch {epoch}/{config['epochs']}", unit="batch", leave=False)
    for i, (imgs, _) in enumerate(progress_bar):
        imgs = imgs.to(device)
        batch_size = imgs.size(0)
        n_batches += 1

        # Ground truths
        valid = torch.ones(batch_size, device=device)
        fake = torch.zeros(batch_size, device=device)

        # -----------------
        #  Train Generator
        # -----------------
        optimizer_g.zero_grad()
        noise = torch.randn(batch_size, config["latent_dim"], 1, 1, device=device)
        gen_imgs = generator(noise)
        g_loss = F.binary_cross_entropy(discriminator(gen_imgs), valid)
        g_loss.backward()
        optimizer_g.step()

        # ---------------------
        #  Train Discriminator
        # ---------------------
        optimizer_d.zero_grad()
        real_loss = F.binary_cross_entropy(discriminator(imgs), valid)
        fake_loss = F.binary_cross_entropy(discriminator(gen_imgs.detach()), fake)
        d_loss = (real_loss + fake_loss) / 2
        d_loss.backward()
        optimizer_d.step()

        total_g_loss += g_loss.item()
        total_d_loss += d_loss.item()

        progress_bar.set_postfix(g_loss=g_loss.item(), d_loss=d_loss.item())

        if config.get("log_interval", -1) != -1 and i % config["log_interval"] == 0:
            wandb.log({
                "batch_g_loss": g_loss.item(),
                "batch_d_loss": d_loss.item(),
                "epoch": epoch,
                "batch": i
            }, step=global_step)
        
        global_step += 1

    # Compute average losses for the epoch
    avg_g_loss = total_g_loss / n_batches
    avg_d_loss = total_d_loss / n_batches
    wandb.log({
        "epoch_avg_g_loss": avg_g_loss,
        "epoch_avg_d_loss": avg_d_loss,
        "epoch": epoch
    }, step=global_step)
    
    logger.info(f"Epoch {epoch} finished: avg_g_loss={avg_g_loss:.4f}, avg_d_loss={avg_d_loss:.4f}")
    return global_step


# ------------------------------
# Evaluation Function
# ------------------------------
def evaluate(generator, device, config, step):
    generator.eval()
    with torch.no_grad():
        noise = torch.randn(config["num_eval_samples"], config["latent_dim"], 1, 1, device=device)
        gen_imgs = generator(noise)
        grid = torchvision.utils.make_grid(gen_imgs, nrow=4, normalize=True)
        wandb.log({"generated_images": [wandb.Image(grid, caption="Generated Images")]}, step=step)
    generator.train()


# ------------------------------
# DataLoader Initialization for CelebA
# ------------------------------
def get_dataloader(config):
    transform = transforms.Compose([
        transforms.CenterCrop(178),            # Crop the faces to square
        transforms.Resize(config["image_size"]), # Resize to the desired image size (e.g., 64x64)
        transforms.ToTensor(),
        transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))
    ])
    dataset = datasets.CelebA(root=config["data_root"], split="train", transform=transform, download=True)
    
    # Use a subset of the dataset
    subset_size = config.get("subset_size", len(dataset))
    indices = torch.randperm(len(dataset))[:subset_size]
    subset = torch.utils.data.Subset(dataset, indices)
    
    return DataLoader(subset, batch_size=config["batch_size"], shuffle=True, num_workers=0)


def generate_run_name(config):
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return config.get("run_name", f"DCGAN_CelebA_lr{config['lr']}_bs{config['batch_size']}_{timestamp}")


# ------------------------------
# Main Training Function
# ------------------------------
def main(config):
    if config["use_cuda"] and not torch.cuda.is_available():
        raise RuntimeError("CUDA is not available, but 'use_cuda' is set to True.")
    
    device = torch.device("cuda" if config["use_cuda"] and torch.cuda.is_available() else "cpu")
    
    # Initialize models and optimizers
    generator = Generator(config).to(device)
    discriminator = Discriminator(config).to(device)
    optimizer_g = torch.optim.Adam(generator.parameters(), lr=config["lr"], betas=(config["beta1"], 0.999), weight_decay=config["weight_decay"])
    optimizer_d = torch.optim.Adam(discriminator.parameters(), lr=config["lr"], betas=(config["beta1"], 0.999), weight_decay=config["weight_decay"])
    
    dataloader = get_dataloader(config)
    
    # Create a unique checkpoint folder based on hyperparameters
    checkpoint_folder = generate_checkpoint_folder(config)
    
    start_epoch = 0
    run_id = None
    global_step = 0
    if os.path.exists(checkpoint_folder) and config.get("resume_training", True):
        start_epoch, global_step, run_id = load_checkpoint(checkpoint_folder, generator, discriminator, optimizer_g, optimizer_d, device, config)

    # Generate a meaningful run name if not provided in config
    run_name = generate_run_name(config)
    tags = config.get("tags", [])

    wandb.init(project=config["wandb_project"], config=config, resume="allow", id=run_id, name=run_name, tags=tags)
    wandb.watch(generator, log="all")
    wandb.watch(discriminator, log="all")

    try:
        for epoch in range(start_epoch, config["epochs"]):
            global_step = train_epoch(generator, discriminator, optimizer_g, optimizer_d, dataloader, device, config, epoch, global_step)
            
            if epoch % config["checkpoint_interval"] == 0 or epoch == config["epochs"] - 1:
                evaluate(generator, device, config, step=global_step)
                save_checkpoint(checkpoint_folder, generator, discriminator, optimizer_g, optimizer_d, epoch, global_step, config)
    finally:
        wandb.finish()
        logger.info("WandB run finished.")


# ------------------------------
# Main Execution
# ------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train a DCGAN on CelebA")
    parser.add_argument("--config", type=str, default="config.yaml", help="Path to YAML configuration file")
    args = parser.parse_args()

    with open(args.config, "r") as f:
        config = yaml.safe_load(f)

    main(config)
