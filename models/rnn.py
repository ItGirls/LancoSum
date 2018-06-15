import torch
import torch.nn as nn
from torch.autograd import Variable
from torch.nn.utils.rnn import pack_padded_sequence as pack
from torch.nn.utils.rnn import pad_packed_sequence as unpack
import models
import math
import numpy as np

class PositionalEncoding(nn.Module):
    """
    Implements the sinusoidal positional encoding for
    non-recurrent neural networks.

    Implementation based on "Attention Is All You Need"
    :cite:`DBLP:journals/corr/VaswaniSPUJGKP17`

    Args:
       dropout (float): dropout parameter
       dim (int): embedding size
    """

    def __init__(self, config, max_len=5000):
        pe = torch.zeros(max_len, config.emb_size)
        position = torch.arange(0, max_len).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, config.emb_size, 2) *
                             -(math.log(10000.0) / config.emb_size))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(1)
        super(PositionalEncoding, self).__init__()
        self.register_buffer('pe', pe)
        self.dropout = nn.Dropout(p=config.dropout)
        self.emb_size = config.emb_size

    def forward(self, emb):
        # We must wrap the self.pe in Variable to compute, not the other
        # way - unwrap emb(i.e. emb.data). Otherwise the computation
        # wouldn't be watched to build the compute graph.
        emb = emb * math.sqrt(self.emb_size)
        # print(self.pe.size())
        emb = emb + Variable(self.pe[:emb.size(0)], requires_grad=False)
        emb = self.dropout(emb)
        return emb


class rnn_encoder(nn.Module):

    def __init__(self, config, embedding=None):
        super(rnn_encoder, self).__init__()

        self.embedding = embedding if embedding is not None else nn.Embedding(config.src_vocab_size, config.emb_size)
        self.hidden_size = config.hidden_size
        self.config = config
        self.dropout = nn.Dropout(p=config.dropout)
        self.sw1 = nn.Sequential(nn.Conv1d(config.hidden_size, config.hidden_size, kernel_size=1, padding=0), nn.BatchNorm1d(config.hidden_size), nn.ReLU())
        self.sw3 = nn.Sequential(nn.Conv1d(config.hidden_size, config.hidden_size, kernel_size=1, padding=0), nn.ReLU(), nn.BatchNorm1d(config.hidden_size), nn.Conv1d(config.hidden_size, config.hidden_size, kernel_size=3, padding=1), nn.ReLU(), nn.BatchNorm1d(config.hidden_size))
        self.sw33 = nn.Sequential(nn.Conv1d(config.hidden_size, config.hidden_size, kernel_size=1, padding=0), nn.ReLU(), nn.BatchNorm1d(config.hidden_size), nn.Conv1d(config.hidden_size, config.hidden_size, kernel_size=3, padding=1), nn.ReLU(), nn.BatchNorm1d(config.hidden_size), nn.Conv1d(config.hidden_size, config.hidden_size, kernel_size=3, padding=1), nn.ReLU(), nn.BatchNorm1d(config.hidden_size))
        self.swish = nn.Conv1d(config.hidden_size, config.hidden_size, kernel_size=5, padding=2)
        self.linear = nn.Sequential(nn.Linear(2*config.hidden_size, 2*config.hidden_size), nn.GLU(), nn.Dropout(config.dropout))
        self.filter_linear = nn.Linear(3*config.hidden_size, config.hidden_size)
        self.tanh = nn.Tanh()
        self.sigmoid = nn.Sigmoid()
        self.PosEnc = PositionalEncoding(config)
        if config.attention == 'None':
            self.attention = None
        elif config.attention == 'bahdanau':
            self.attention = models.bahdanau_attention(config.hidden_size, config.emb_size, config.pool_size)
        elif config.attention == 'luong':
            self.attention = models.luong_attention(config.hidden_size, config.emb_size, config.pool_size)
        elif config.attention == 'luong_gate':
            self.attention = models.luong_gate_attention(config.hidden_size, config.emb_size, prob=config.dropout)
        if config.cell == 'gru':
            self.rnn = nn.GRU(input_size=config.emb_size, hidden_size=config.hidden_size,
                              num_layers=config.enc_num_layers, dropout=config.dropout,
                              bidirectional=config.bidirectional)
        else:
            self.rnn = nn.LSTM(input_size=config.emb_size, hidden_size=config.hidden_size,
                               num_layers=config.enc_num_layers, dropout=config.dropout,
                               bidirectional=config.bidirectional)

    def forward(self, inputs, lengths):
        embs = pack(self.embedding(inputs), lengths)
        embeds = unpack(embs)[0]
        outputs, state = self.rnn(embs)
        outputs = unpack(outputs)[0]
        if self.config.bidirectional:
            if self.config.swish:
                outputs = self.linear(outputs)
            else:
                outputs = outputs[:,:,:self.config.hidden_size] + outputs[:,:,self.config.hidden_size:]

        if self.config.gated:
            outputs = self.tanh(outputs / 0.7) * outputs
            outputs = self.sigmoid(outputs) * outputs

        if self.config.swish:
            outputs = self.PosEnc(outputs)
            outputs = outputs.transpose(0,1).transpose(1,2)
            conv1 = self.sw1(outputs)
            conv3 = self.sw3(outputs)
            conv33 = self.sw33(outputs)
            conv = torch.cat((conv1, conv3, conv33), 1)
            conv = self.filter_linear(conv.transpose(1,2))
            if self.config.selfatt:
                conv = conv.transpose(0,1)
                outputs = outputs.transpose(1,2).transpose(0,1)
            else:
                gate = self.sigmoid(conv)
                outputs = outputs * gate.transpose(1,2)
                outputs = outputs.transpose(1,2).transpose(0,1)

        if self.config.selfatt:
            self.attention.init_context(context=outputs)
            out_attn = []
            for i, out in enumerate(conv.split(1)): 
                output, weights = self.attention(out.squeeze(0), selfatt=True)
                out_attn.append(output)
            out_attn = torch.stack(out_attn)
            gate = self.sigmoid(out_attn)
            outputs = outputs * gate

        if self.config.cell == 'gru':
            state = state[:self.config.dec_num_layers]
        else:
            state = (state[0][:self.config.dec_num_layers], state[1][:self.config.dec_num_layers])

        return outputs, state


class rnn_decoder(nn.Module):

    def __init__(self, config, embedding=None, use_attention=True, score_fn=None):
        super(rnn_decoder, self).__init__()
        self.embedding = embedding if embedding is not None else nn.Embedding(config.tgt_vocab_size, config.emb_size)

        input_size = config.emb_size

        if config.cell == 'gru':
            self.rnn = StackedGRU(input_size=input_size, hidden_size=config.hidden_size,
                                  num_layers=config.dec_num_layers, dropout=config.dropout)
        else:
            self.rnn = StackedLSTM(input_size=input_size, hidden_size=config.hidden_size,
                                   num_layers=config.dec_num_layers, dropout=config.dropout)

        if score_fn.startswith('general'):
            self.linear = nn.Linear(config.hidden_size, config.emb_size)
            if score_fn.endswith('not'):
                self.score_fn = lambda x: torch.matmul(self.linear(x), Variable(self.embedding.weight.t().data))
            else:
                self.score_fn = lambda x: torch.matmul(self.linear(x), self.embedding.weight.t())
        elif score_fn.startswith('dot'):
            if score_fn.endswith('not'):
                self.score_fn = lambda x: torch.matmul(x, Variable(self.embedding.weight.t().data))
            else:
                self.score_fn = lambda x: torch.matmul(x, self.embedding.weight.t())
        else:
            self.score_fn = nn.Linear(config.hidden_size, config.tgt_vocab_size)

        self.sigmoid = nn.Sigmoid()

        if not use_attention or config.attention == 'None':
            self.attention = None
        elif config.attention == 'bahdanau':
            self.attention = models.bahdanau_attention(config.hidden_size, config.emb_size, config.pool_size)
        elif config.attention == 'luong':
            self.attention = models.luong_attention(config.hidden_size, config.emb_size, config.pool_size)
        elif config.attention == 'luong_gate':
            self.attention = models.luong_gate_attention(config.hidden_size, config.emb_size, prob=config.dropout)

        self.hidden_size = config.hidden_size
        self.dropout = nn.Dropout(config.dropout)
        self.config = config

    def forward(self, input, state, attention=True):
        embs = self.embedding(input)
        output, state = self.rnn(embs, state)
        if self.attention is not None and attention:
            if self.config.attention == 'luong_gate':
                output, attn_weights = self.attention(output)
            else:
                output, attn_weights = self.attention(output, embs)
        else:
            attn_weights = None
        output = self.compute_score(output)

        return output, state, attn_weights

    def compute_score(self, hiddens):
        scores = self.score_fn(hiddens)
        return scores


class StackedLSTM(nn.Module):
    def __init__(self, num_layers, input_size, hidden_size, dropout):
        super(StackedLSTM, self).__init__()
        self.dropout = nn.Dropout(dropout)
        self.num_layers = num_layers
        self.layers = nn.ModuleList()

        for i in range(num_layers):
            self.layers.append(nn.LSTMCell(input_size, hidden_size))
            input_size = hidden_size

    def forward(self, input, hidden):
        h_0, c_0 = hidden
        h_1, c_1 = [], []
        for i, layer in enumerate(self.layers):
            h_1_i, c_1_i = layer(input, (h_0[i], c_0[i]))
            input = h_1_i
            if i + 1 != self.num_layers:
                input = self.dropout(input)
            h_1 += [h_1_i]
            c_1 += [c_1_i]

        h_1 = torch.stack(h_1)
        c_1 = torch.stack(c_1)

        return input, (h_1, c_1)


class StackedGRU(nn.Module):
    def __init__(self, num_layers, input_size, hidden_size, dropout):
        super(StackedGRU, self).__init__()
        self.dropout = nn.Dropout(dropout)
        self.num_layers = num_layers
        self.layers = nn.ModuleList()

        for i in range(num_layers):
            self.layers.append(nn.GRUCell(input_size, hidden_size))
            input_size = hidden_size

    def forward(self, input, hidden):
        h_0 = hidden
        h_1 = []
        for i, layer in enumerate(self.layers):
            h_1_i = layer(input, h_0[i])
            input = h_1_i
            if i + 1 != self.num_layers:
                input = self.dropout(input)
            h_1 += [h_1_i]

        h_1 = torch.stack(h_1)

        return input, h_1
