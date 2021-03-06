import os
import random
from datetime import datetime
import numpy as np
from scipy.signal import savgol_filter
import torch
from torch import nn
from torch.utils.data.dataset import Dataset
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
import torchaudio


ROOT_PATH = os.getenv('SURAT_ROOT_PATH', False)
if not ROOT_PATH:
    ROOT_PATH = os.path.expanduser(os.path.join('~', 'sandbox', 'surat'))
DEVICE = torch.device('cuda')
OUTPUT_COUNT = 8320 * 3  # 8320 vertex positions in 3 dimentions


class Data(Dataset):
    def __init__(self, transforms=None, shiftRandom=True, validationAudioPath=None):
        self.transforms = transforms
        self.preview = validationAudioPath is not None
        self.shiftRandom = shiftRandom and not self.preview
        self.count = None

        animFPS = 29.97  # samSoar recorded with an ipad

        if self.preview:
            inputSpeechPath = validationAudioPath
        else:
            inputSpeechPath = os.path.join(ROOT_PATH, 'data', 'samSoar', 'samSoar.wav')
        waveform, sampleRate = torchaudio.load(inputSpeechPath)
        if sampleRate != 16000:
            waveform = torchaudio.transforms.Resample(sampleRate, 16000)(waveform)
            sampleRate = 16000

        self.count = int(animFPS * (waveform.size()[1] / sampleRate))

        # remove DC component
        waveform -= torch.mean(waveform)

        self.MFCC = torchaudio.compliance.kaldi.mfcc(
            waveform,
            channel=0,
            remove_dc_offset=True,
            window_type='hanning',
            num_ceps=32,
            num_mel_bins=64,
            frame_length=256,
            frame_shift=32
        )
        self.MFCCLen = self.MFCC.size()[0]

    def __getitem__(self, i):
        if i < 0:  # for negative indexing
            i = self.count + i

        if self.shiftRandom:
            randomShift = random.randint(0, 1)  # frame length 64 is about 8 ms
        else:
            randomShift = 0
        audioIdxRoll = int(i * (self.MFCCLen / self.count) + randomShift)
        audioIdxRollPair = int((i + 1) * (self.MFCCLen / self.count) + randomShift)
        if audioIdxRoll < 32 or audioIdxRollPair < 32 or audioIdxRoll + 32 > self.MFCCLen or audioIdxRollPair + 32 > self.MFCCLen:
            inputValue = (
                torch.cat(
                    (
                        torch.cat(
                            (
                                torch.roll(
                                    self.MFCC,
                                    (audioIdxRoll * -1) + 32,
                                    dims=0,
                                )[:32],
                                torch.roll(
                                    self.MFCC,
                                    (audioIdxRoll * -1),
                                    dims=0,
                                )[:32],
                            ),
                            dim=0,
                        ),
                        torch.cat(
                            (
                                torch.roll(
                                    self.MFCC,
                                    (audioIdxRollPair * -1) + 32,
                                    dims=0,
                                )[:32],
                                torch.roll(
                                    self.MFCC,
                                    (audioIdxRollPair * -1),
                                    dims=0,
                                )[:32],
                            ),
                            dim=0,
                        ),
                    ),
                    dim=0,
                )
                .view(2, 1, 64, 32)
                .float()

            )
        else:
            inputValue = (
                torch.cat(
                    (
                        self.MFCC[audioIdxRoll - 32: audioIdxRoll + 32],
                        self.MFCC[audioIdxRollPair - 32: audioIdxRollPair + 32]
                    ),
                    dim=0,
                )
                .view(2, 1, 64, 32)
                .float()

            )

        if self.preview:
            return (
                torch.Tensor([i]).long(),
                inputValue[0],
                torch.zeros((1, OUTPUT_COUNT))
            )

        targetValue = torch.from_numpy(np.append(
            np.load(
                os.path.join(
                    ROOT_PATH,
                    'data', 'samSoar', 'maskSeq',
                    'mask.{:05d}.npy'.format(i + 1)
                )
            ),
            np.load(
                os.path.join(
                    ROOT_PATH,
                    'data', 'samSoar', 'maskSeq',
                    'mask.{:05d}.npy'.format(i + 2)
                )
            )
        )).float().view(-1, OUTPUT_COUNT)

        return (
            torch.Tensor([i]).long(),
            inputValue,
            (targetValue) * .5  # output values are assumed to have max of 2 and min of -2
        )

    def __len__(self):
        if self.preview:
            return self.count
        return self.count - 1  # for pairs

class Model(nn.Module):
    def __init__(self, moodSize, filterMood=False):
        super(Model, self).__init__()

        self.formantAnalysis = nn.Sequential(
            nn.Conv2d(1, 72, (1, 3), (1, 2), (0, 1), 1),
            nn.BatchNorm2d(72),
            nn.LeakyReLU(),
            nn.Conv2d(72, 108, (1, 3), (1, 2), (0, 1), 1),
            nn.BatchNorm2d(108),
            nn.LeakyReLU(),
            nn.Conv2d(108, 162, (1, 3), (1, 2), (0, 1), 1),
            nn.BatchNorm2d(162),
            nn.LeakyReLU(),
            nn.Conv2d(162, 243, (1, 3), (1, 2), (0, 1), 1),
            nn.BatchNorm2d(243),
            nn.LeakyReLU(),
            nn.Conv2d(243, 256, (1, 2), (1, 2)),
            nn.BatchNorm2d(256),
            nn.LeakyReLU(),
        )

        self.moodLen = 16
        mood = np.random.normal(.0, 1., (moodSize, self.moodLen))
        if filterMood:
            mood = savgol_filter(mood, 129, 2, axis=0)
        self.mood = nn.Parameter(
            torch.from_numpy(mood).float().view(moodSize, self.moodLen).to(DEVICE),
            requires_grad=True
        )

        self.articulation = nn.Sequential(
            nn.Conv2d(
                256 + self.moodLen, 256 + self.moodLen, (3, 1), (2, 1), (1, 0), 1
            ),
            nn.BatchNorm2d(256 + self.moodLen, 0.8),
            nn.LeakyReLU(),
            nn.Dropout2d(0.2),
            nn.Conv2d(
                256 + self.moodLen, 256 + self.moodLen, (3, 1), (2, 1), (1, 0), 1
            ),
            nn.BatchNorm2d(256 + self.moodLen, 0.8),
            nn.LeakyReLU(),
            nn.Dropout2d(0.2),
            nn.Conv2d(
                256 + self.moodLen, 256 + self.moodLen, (3, 1), (2, 1), (1, 0), 1
            ),
            nn.BatchNorm2d(256 + self.moodLen, 0.8),
            nn.LeakyReLU(),
            nn.Dropout2d(0.2),
            nn.Conv2d(
                256 + self.moodLen, 256 + self.moodLen, (3, 1), (2, 1), (1, 0), 1
            ),
            nn.BatchNorm2d(256 + self.moodLen, 0.8),
            nn.LeakyReLU(),
            nn.Dropout2d(0.2),
            nn.Conv2d(
                256 + self.moodLen, 256 + self.moodLen, (4, 1), (4, 1), (1, 0), 1
            ),
            nn.BatchNorm2d(256 + self.moodLen, 0.8),
            nn.LeakyReLU(),
            nn.Dropout2d(0.2),
        )
        self.output = nn.Sequential(
            nn.Linear(256 + self.moodLen, 128),
            nn.Linear(128, OUTPUT_COUNT),
            nn.Tanh(),
        )

    def forward(self, inp, mood, moodIndex=0):
        out = self.formantAnalysis(inp)
        if mood is not None:
            out = torch.cat(
                (
                    out,
                    mood.view(
                        mood.view(-1, self.moodLen).size()[0], self.moodLen, 1, 1
                    ).expand(out.size()[0], self.moodLen, 64, 1)
                ),
                dim=1
            ).view(-1, 256 + self.moodLen, 64, 1)
        else:
            out = torch.cat(
                (
                    out,
                    torch.cat((
                        self.mood[moodIndex.chunk(chunks=1, dim=0)],
                        self.mood[(moodIndex + 1).chunk(chunks=1, dim=0)]
                    ), dim=0).view(
                        out.size()[0], self.moodLen, 1, 1
                    ).expand(out.size()[0], self.moodLen, 64, 1)
                ),
                dim=1
            ).view(-1, 256 + self.moodLen, 64, 1)
        out = self.articulation(out)
        out = self.output(out.view(-1, 256 + self.moodLen))
        return out.view(-1, OUTPUT_COUNT)


def train():
    batchSize = 1024
    dataSet = Data()
    dataLoader = DataLoader(
        dataset=dataSet,
        batch_size=batchSize,
        shuffle=True,
        num_workers=8
    )

    model = Model(dataSet.count).to(DEVICE)
    modelOptimizer = torch.optim.Adam(
        model.parameters(),
        lr=1e-3
    )

    epochCount = 50000

    runStr = datetime.now().strftime('%y_%m_%d_%H_%M_%S')
    logWriter = SummaryWriter(os.path.join(ROOT_PATH, 'logs', runStr))

    modelDir = os.path.join(ROOT_PATH, 'model', runStr)
    if not os.path.exists(modelDir):
        os.makedirs(modelDir)

    MSENoReductionCriterion = torch.nn.MSELoss(reduction='none').to(DEVICE)
    for epochIdx in range(epochCount):
        for i, inputData, target in dataLoader:
            i = i.to(DEVICE)
            inputData = inputData.to(DEVICE)
            target = target.to(DEVICE)
            # compensate for paired input
            inputData = inputData.view(-1, 1, 64, 32)
            target = target.view(-1, OUTPUT_COUNT)
            targetPairView = target.view(-1, 2, OUTPUT_COUNT)

            modelOptimizer.zero_grad()
            modelResult = model(inputData, None, i)
            modelResultPairView = modelResult.view(-1, 2, OUTPUT_COUNT)

            shapeLoss = torch.mean(torch.sum(
                MSENoReductionCriterion(
                    modelResultPairView[:, 0, :],
                    targetPairView[:, 0, :]
                ),
                dim=-1
            ))

            motionLoss = torch.mean(torch.sum(
                MSENoReductionCriterion(
                    modelResultPairView[:, 1, :] - modelResultPairView[:, 0, :],
                    targetPairView[:, 1, :] - targetPairView[:, 0, :],
                ),
                dim=-1
            ))

            emotionLoss = torch.mean(torch.sum(
                MSENoReductionCriterion(
                    model.mood[i],
                    model.mood[i + 1]
                ),
                dim=-1
            ))

            (shapeLoss + motionLoss + emotionLoss).backward()
            modelOptimizer.step()

        logWriter.add_scalar('emotion', emotionLoss.item(), epochIdx + 1)
        logWriter.add_scalar('motion', motionLoss.item(), epochIdx + 1)
        logWriter.add_scalar('shape', shapeLoss.item(), epochIdx + 1)

        if (epochIdx + 1) % 50 == 0:
            torch.save(
                model.state_dict(),
                os.path.join(modelDir, '{}_E{:05d}.pth'.format(runStr, epochIdx + 1)),
            )

    torch.save(model.state_dict(), os.path.join(modelDir, '{}_fin.pth'.format(runStr)))



if __name__ == '__main__':
    print('start: {}'.format(datetime.now()))
    start = datetime.now()
    print('training')
    train()
    print('done')
    print('duration: {}'.format(datetime.now() - start))
