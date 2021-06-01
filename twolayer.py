# -*- coding: utf-8 -*-
"""attention.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1iUO3JQJppZI4ICPeTIUe9h1YNwypYVJe

train data preprocess
"""

seed = 2020

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader

import time
import math
import random

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

"""read data

"""

# 每一行数据如下
# 'Hi.\t嗨。\tCC-BY 2.0 (France) Attribution: tatoeba.org #538123 (CM) & #891077 (Martha)'
with open('newdata', 'r', encoding='utf-8') as f:
    data = f.read()
data = data.strip()
data = data.split('\n')
print('样本数:\n', len(data))
print('\n样本示例:')
data[0]

en_data = [line.split('\t')[0] for line in data]
ch_data = [line.split('\t')[1] for line in data]
print('英文数据:\n', en_data[:10])
print('\n德文数据:\n', ch_data[:10])

# 按字符级切割，并添加<eos>
en_token_list = [[char for char in line]+["<eos>"] for line in en_data]
ch_token_list = [[char for char in line]+["<eos>"] for line in ch_data]
print('英文数据:\n', en_token_list[:2])
print('\n德文数据:\n', ch_token_list[:2])

# 基本字典
basic_dict = {'<pad>':0, '<unk>':1, '<bos>':2, '<eos>':3}
# 分别生成德英文字典 
en_vocab = set(''.join(en_data))
en2id = {char:i+len(basic_dict) for i, char in enumerate(en_vocab)}
en2id.update(basic_dict)
id2en = {v:k for k,v in en2id.items()}

# 分别生成德英文字典 
ch_vocab = set(''.join(ch_data))
ch2id = {char:i+len(basic_dict) for i, char in enumerate(ch_vocab)}
ch2id.update(basic_dict)
id2ch = {v:k for k,v in ch2id.items()}

# 利用字典，映射数据 
en_num_data = [[en2id[en] for en in line ] for line in en_token_list]
ch_num_data = [[ch2id[ch] for ch in line] for line in ch_token_list]

print('char:', en_data[1])
print('index:', en_num_data[1])

class TranslationDataset(Dataset):
    def __init__(self, src_data, trg_data):
        self.src_data = src_data
        self.trg_data = trg_data

        assert len(src_data) == len(trg_data), \
            "numbers of src_data  and trg_data must be equal!"

    def __len__(self):
        return len(self.src_data)

    def __getitem__(self, idx):
        src_sample =self.src_data[idx]
        src_len = len(self.src_data[idx])
        trg_sample = self.trg_data[idx]
        trg_len = len(self.trg_data[idx])
        return {"src": src_sample, "src_len": src_len, "trg": trg_sample, "trg_len": trg_len}

def padding_batch(batch):
    """
    input: -> list of dict
        [{'src': [1, 2, 3], 'trg': [1, 2, 3]}, {'src': [1, 2, 2, 3], 'trg': [1, 2, 2, 3]}]
    output: -> dict of tensor 
        {
            "src": [[1, 2, 3, 0], [1, 2, 2, 3]].T
            "trg": [[1, 2, 3, 0], [1, 2, 2, 3]].T
        }
    """
    src_lens = [d["src_len"] for d in batch]
    trg_lens = [d["trg_len"] for d in batch]
    
    src_max = max([d["src_len"] for d in batch])
    trg_max = max([d["trg_len"] for d in batch])
    for d in batch:
        d["src"].extend([en2id["<pad>"]]*(src_max-d["src_len"]))
        d["trg"].extend([ch2id["<pad>"]]*(trg_max-d["trg_len"]))
    srcs = torch.tensor([pair["src"] for pair in batch], dtype=torch.long, device=device)
    trgs = torch.tensor([pair["trg"] for pair in batch], dtype=torch.long, device=device)
    
    batch = {"src":srcs.T, "src_len":src_lens, "trg":trgs.T, "trg_len":trg_lens}
    return batch

"""attention model"""

class Encoder(nn.Module):
    def __init__(self, input_dim, emb_dim, hid_dim, n_layers, dropout=0.5, bidirectional=True):
        super(Encoder, self).__init__()
        
        self.hid_dim = hid_dim
        self.n_layers = n_layers
        
        self.embedding = nn.Embedding(input_dim, emb_dim)
        self.gru = nn.GRU(emb_dim, hid_dim, n_layers, dropout=dropout, bidirectional=bidirectional)
        
    def forward(self, input_seqs, input_lengths, hidden):
        # input_seqs = [seq_len, batch]
        embedded = self.embedding(input_seqs)
        # embedded = [seq_len, batch, embed_dim]
        packed = torch.nn.utils.rnn.pack_padded_sequence(embedded, input_lengths, enforce_sorted=False)
        
        outputs, hidden = self.gru(packed, hidden)        
        outputs, output_lengths = torch.nn.utils.rnn.pad_packed_sequence(outputs)
        # outputs = [seq_len, batch, hid_dim * n directions]
        # output_lengths = [batch]
        return outputs, hidden

class Attn(nn.Module):
    def __init__(self, method, hidden_size):
        super(Attn, self).__init__()
        self.method = method
        if self.method not in ['dot', 'general', 'concat']:
            raise ValueError(self.method, "is not an appropriate attention method.")
        self.hidden_size = hidden_size
        if self.method == 'general':
            self.attn = nn.Linear(self.hidden_size, hidden_size)
        elif self.method == 'concat':
            self.attn = nn.Linear(self.hidden_size * 2, hidden_size)
            self.v = nn.Parameter(torch.FloatTensor(hidden_size))

    def dot_score(self, hidden, encoder_output):
        return torch.sum(hidden * encoder_output, dim=2)  # [seq_len, batch]

    def general_score(self, hidden, encoder_output):
        energy = self.attn(encoder_output)  # [seq_len, batch, hid_dim]
        return torch.sum(hidden * energy, dim=2)  # [seq_len, batch]

    def concat_score(self, hidden, encoder_output):
        # hidden.expand(encoder_output.size(0), -1, -1) -> [seq_len, batch, N]
        energy = self.attn(torch.cat((hidden.expand(encoder_output.size(0), -1, -1), encoder_output), 2)).tanh()
        # energy = [sql_len, batch, hidden_size]
        return torch.sum(self.v * energy, dim=2)  # [seq_len, batch]

    def forward(self, hidden, encoder_outputs):
        # hidden = [1, batch,  n_directions * hid_dim]
        # encoder_outputs = [seq_len, batch, hid dim * n directions]
        if self.method == 'general':
            attn_energies = self.general_score(hidden, encoder_outputs)
        elif self.method == 'concat':
            attn_energies = self.concat_score(hidden, encoder_outputs)
        elif self.method == 'dot':
            attn_energies = self.dot_score(hidden, encoder_outputs)

        attn_energies = attn_energies.t()  # [batch, seq_len]
 
        return F.softmax(attn_energies, dim=1).unsqueeze(1)  # softmax归一化# [batch, 1, seq_len]

class AttnDecoder(nn.Module):
    def __init__(self, output_dim, emb_dim, hid_dim, n_layers=1, dropout=0.5, bidirectional=True, attn_method="general"):
        super(AttnDecoder, self).__init__()

        self.output_dim = output_dim
        self.emb_dim = emb_dim
        self.hid_dim = hid_dim
        self.n_layers = n_layers
        self.dropout = dropout

        self.embedding = nn.Embedding(output_dim, emb_dim)
        self.embedding_dropout = nn.Dropout(dropout)
        self.gru = nn.GRU(emb_dim, hid_dim, n_layers, dropout=dropout, bidirectional=bidirectional)
        
        if bidirectional:
            self.concat = nn.Linear(hid_dim * 2 * 2, hid_dim*2)
            self.out = nn.Linear(hid_dim*2, output_dim)
            self.attn = Attn(attn_method, hid_dim*2)
        else:
            self.concat = nn.Linear(hid_dim * 2, hid_dim)
            self.out = nn.Linear(hid_dim, output_dim)
            self.attn = Attn(attn_method, hid_dim)
        self.softmax = nn.LogSoftmax(dim=1)

    def forward(self, token_inputs, last_hidden, encoder_outputs):
        batch_size = token_inputs.size(0)
        embedded = self.embedding(token_inputs)
        embedded = self.embedding_dropout(embedded)
        embedded = embedded.view(1, batch_size, -1) # [1, B, hid_dim]

        gru_output, hidden = self.gru(embedded, last_hidden)
        # gru_output = [1, batch,  n_directions * hid_dim]
        # hidden = [n_layers * n_directions, batch, hid_dim]

        # encoder_outputs = [sql_len, batch, hid dim * n directions]
        attn_weights = self.attn(gru_output, encoder_outputs)
        # attn_weights = [batch, 1, sql_len]
        context = attn_weights.bmm(encoder_outputs.transpose(0, 1))
        # [batch, 1, hid_dim * n directions]

        # LuongAttention
        gru_output = gru_output.squeeze(0) # [batch, n_directions * hid_dim]
        context = context.squeeze(1)       # [batch, n_directions * hid_dim]
        concat_input = torch.cat((gru_output, context), 1)  # [batch, n_directions * hid_dim * 2]
        concat_output = torch.tanh(self.concat(concat_input))  # [batch, n_directions*hid_dim]

        output = self.out(concat_output)  # [batch, output_dim]
        output = self.softmax(output)

        return output, hidden, attn_weights

class Seq2Seq(nn.Module):
    def __init__(self, 
                 encoder, 
                 decoder, 
                 device, 
                 predict=False, 
                 basic_dict=None,
                 max_len=100
                 ):
        super(Seq2Seq, self).__init__()
        
        self.device = device

        self.encoder = encoder
        self.decoder = decoder

        self.predict = predict  # 训练阶段还是预测阶段
        self.basic_dict = basic_dict  # decoder的字典，存放特殊token对应的id
        self.max_len = max_len  # 翻译时最大输出长度

        assert encoder.hid_dim == decoder.hid_dim, \
            "Hidden dimensions of encoder and decoder must be equal!"
        assert encoder.n_layers == decoder.n_layers, \
            "Encoder and decoder must have equal number of layers!"
        assert encoder.gru.bidirectional == decoder.gru.bidirectional, \
            "Decoder and encoder must had same value of bidirectional attribute!"
        
    def forward(self, input_batches, input_lengths, target_batches=None, target_lengths=None, teacher_forcing_ratio=0.5):
        # input_batches = [seq_len, batch]
        # target_batches = [seq_len, batch]
        batch_size = input_batches.size(1)
        
        BOS_token = self.basic_dict["<bos>"]
        EOS_token = self.basic_dict["<eos>"]
        PAD_token = self.basic_dict["<pad>"]

        # 初始化
        enc_n_layers = self.encoder.gru.num_layers
        enc_n_directions = 2 if self.encoder.gru.bidirectional else 1
        encoder_hidden = torch.zeros(enc_n_layers*enc_n_directions, batch_size, self.encoder.hid_dim, device=self.device)
        
        # encoder_outputs = [input_lengths, batch, hid_dim * n directions]
        # encoder_hidden = [n_layers*n_directions, batch, hid_dim]
        encoder_outputs, encoder_hidden = self.encoder(
            input_batches, input_lengths, encoder_hidden)

        # 初始化
        decoder_input = torch.tensor([BOS_token] * batch_size, dtype=torch.long, device=self.device)
        decoder_hidden = encoder_hidden

        if self.predict:
            # 一次只输入一句话
            assert batch_size == 1, "batch_size of predict phase must be 1!"
            output_tokens = []

            while True:
                decoder_output, decoder_hidden, decoder_attn = self.decoder(
                    decoder_input, decoder_hidden, encoder_outputs
                )
                # [1, 1]
                topv, topi = decoder_output.topk(1)
                decoder_input = topi.squeeze(1).detach()
                output_token = topi.squeeze().detach().item()
                if output_token == EOS_token or len(output_tokens) == self.max_len:
                    break
                output_tokens.append(output_token)
            return output_tokens

        else:
            max_target_length = max(target_lengths)
            all_decoder_outputs = torch.zeros((max_target_length, batch_size, self.decoder.output_dim), device=self.device)

            for t in range(max_target_length):
                use_teacher_forcing = True if random.random() < teacher_forcing_ratio else False
                if use_teacher_forcing:
                    # decoder_output = [batch, output_dim]
                    # decoder_hidden = [n_layers*n_directions, batch, hid_dim]
                    decoder_output, decoder_hidden, decoder_attn = self.decoder(
                        decoder_input, decoder_hidden, encoder_outputs
                    )
                    all_decoder_outputs[t] = decoder_output
                    decoder_input = target_batches[t]  # 下一个输入来自训练数据
                else:
                    decoder_output, decoder_hidden, decoder_attn = self.decoder(
                        decoder_input, decoder_hidden, encoder_outputs
                    )
                    # [batch, 1]
                    topv, topi = decoder_output.topk(1)
                    all_decoder_outputs[t] = decoder_output
                    decoder_input = topi.squeeze(1).detach()  # 下一个输入来自模型预测
            
            loss_fn = nn.NLLLoss(ignore_index=PAD_token)
            loss = loss_fn(
                all_decoder_outputs.reshape(-1, self.decoder.output_dim),  # [batch*seq_len, output_dim]
                target_batches.reshape(-1)               # [batch*seq_len]
            )
            return loss

"""train

"""

def epoch_time(start_time, end_time):
    elapsed_time = end_time - start_time
    elapsed_mins = int(elapsed_time / 60)
    elapsed_secs = int(elapsed_time - (elapsed_mins * 60))
    return elapsed_mins, elapsed_secs

def train(
    model,
    data_loader, 
    optimizer, 
    clip=1, 
    teacher_forcing_ratio=0.5, 
    print_every=None  # None不打印
    ):
    model.predict = False
    model.train()

    if print_every == 0:
        print_every = 1

    print_loss_total = 0  # 每次打印都重置
    start = time.time()
    epoch_loss = 0
    for i, batch in enumerate(data_loader):

        # shape = [seq_len, batch]
        input_batchs = batch["src"]
        target_batchs = batch["trg"]
        # list
        input_lens = batch["src_len"]
        target_lens = batch["trg_len"]
        
        optimizer.zero_grad()
        
        loss = model(input_batchs, input_lens, target_batchs, target_lens, teacher_forcing_ratio)
        print_loss_total += loss.item()
        epoch_loss += loss.item()
        loss.backward()

        # 梯度裁剪
        torch.nn.utils.clip_grad_norm_(model.parameters(), clip)

        optimizer.step()

        if print_every and (i+1) % print_every == 0:
            print_loss_avg = print_loss_total / print_every
            print_loss_total = 0
            print('\tCurrent Loss: %.4f' % print_loss_avg)

    return epoch_loss / len(data_loader)

def evaluate(
    model,
    data_loader, 
    print_every=None
    ):
    model.predict = False
    model.eval()
    if print_every == 0:
        print_every = 1

    print_loss_total = 0  # 每次打印都重置
    start = time.time()
    epoch_loss = 0
    with torch.no_grad():
        for i, batch in enumerate(data_loader):

            # shape = [seq_len, batch]
            input_batchs = batch["src"]
            target_batchs = batch["trg"]
            # list
            input_lens = batch["src_len"]
            target_lens = batch["trg_len"]

            loss = model(input_batchs, input_lens, target_batchs, target_lens, teacher_forcing_ratio=0)
            print_loss_total += loss.item()
            epoch_loss += loss.item()

            if print_every and (i+1) % print_every == 0:
                print_loss_avg = print_loss_total / print_every
                print_loss_total = 0
                print('\tCurrent Loss: %.4f' % print_loss_avg)

    return epoch_loss / len(data_loader)

def translate(
    model,
    sample, 
    idx2token=None
    ):
    model.predict = True
    model.eval()

    # shape = [seq_len, 1]
    input_batch = sample["src"]
    # list
    input_len = sample["src_len"]

    output_tokens = model(input_batch, input_len)
    output_tokens = [idx2token[t] for t in output_tokens]

    return "".join(output_tokens)

INPUT_DIM = len(en2id)
OUTPUT_DIM = len(ch2id)
# 超参数
BATCH_SIZE = 32
ENC_EMB_DIM = 256
DEC_EMB_DIM = 256
HID_DIM = 512
N_LAYERS = 2
ENC_DROPOUT = 0.5
DEC_DROPOUT = 0.5
LEARNING_RATE = 1e-4
N_EPOCHS = 200
CLIP = 1

bidirectional = True
attn_method = "general"
enc = Encoder(INPUT_DIM, ENC_EMB_DIM, HID_DIM, N_LAYERS, ENC_DROPOUT, bidirectional)
dec = AttnDecoder(OUTPUT_DIM, DEC_EMB_DIM, HID_DIM, N_LAYERS, DEC_DROPOUT, bidirectional, attn_method)
model = Seq2Seq(enc, dec, device, basic_dict=basic_dict).to(device)

optimizer = optim.Adam(model.parameters(), lr=LEARNING_RATE)



# 数据集
train_set = TranslationDataset(en_num_data, ch_num_data)
train_loader = DataLoader(train_set, batch_size=BATCH_SIZE, collate_fn=padding_batch)

best_valid_loss = float('inf')

for epoch in range(N_EPOCHS):
    
    start_time = time.time()
    train_loss = train(model, train_loader, optimizer, CLIP)
    valid_loss = evaluate(model, train_loader)
    end_time = time.time()
    
    if valid_loss < best_valid_loss:
        best_valid_loss = valid_loss
        torch.save(model.state_dict(), "en2ch-attn-model2.pt")

    if epoch %2 == 0:
        epoch_mins, epoch_secs = epoch_time(start_time, end_time)
        print(f'Epoch: {epoch+1:02} | Time: {epoch_mins}m {epoch_secs}s')
        print(f'\tTrain Loss: {train_loss:.3f} | Val. Loss: {valid_loss:.3f}')

print("best valid loss：", best_valid_loss)
# 加载最优权重
# model.load_state_dict(torch.load("en2ch-attn-model.pt"))

model.load_state_dict(torch.load("en2ch-attn-model2.pt"))

"""load test data

"""



random.seed(seed)

from tqdm import tqdm

file1=open("Result_twolayer.txt","w",encoding='utf-8')

for i in random.sample(range(len(en_num_data)),len(en_num_data)):  
    en_tokens = list(filter(lambda x: x!=0, en_num_data[i]))  # 过滤零
    ch_tokens = list(filter(lambda x: x!=3 and x!=0, ch_num_data[i]))  # 和机器翻译作对照
    sentence = [id2en[t] for t in en_tokens]
    print("【原文】")
    print("".join(sentence))
    translation = [id2ch[t] for t in ch_tokens]
    print("【原文】")
    print("".join(translation))
    test_sample = {}
    test_sample["src"] = torch.tensor(en_tokens, dtype=torch.long, device=device).reshape(-1, 1)
    test_sample["src_len"] = [len(en_tokens)]
    
    file1.writelines(translate(model, test_sample, id2ch))
    file1.writelines("\n")
    print("【机器翻译】")
    print(translate(model, test_sample, id2ch), end="\n\n")
    
file1.close()

